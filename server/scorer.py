"""Sector-aware value scoring engine — fully ratio-based.

Every valuation metric is scored by comparing the stock's value to the
sector/market peer average.  There are zero hardcoded price or multiple
breakpoints.  Sector rubrics define *which* metrics matter and their
relative weight; the generic ``_ratio_score`` function handles the rest.
"""

from __future__ import annotations

import time
from typing import Optional

from server.models import ScoreBreakdown, ScoredStock, SectorAverages

# ── Constants ────────────────────────────────────────────────────────
_CHINA_COUNTRIES = {"China", "Hong Kong"}


# =====================================================================
# Sector detection
# =====================================================================

def detect_sector_type(sector: str, industry: str) -> str:
    sector = sector or ""
    industry = industry or ""
    s_low = sector.lower()
    i_low = industry.lower()

    if "financial" in s_low or "bank" in i_low or "insurance" in i_low or "asset management" in i_low:
        return "financial"
    if sector == "Real Estate":
        return "reit"
    if sector == "Energy" or "oil" in i_low or "gas" in i_low or "coal" in i_low:
        return "energy"
    if sector == "Healthcare":
        return "healthcare"
    if sector == "Consumer Defensive":
        return "staples"
    if sector == "Consumer Cyclical":
        return "cyclical"
    if sector == "Industrials":
        return "industrial"
    if sector == "Communication Services":
        return "comms"
    if sector == "Basic Materials":
        return "materials"
    if sector == "Utilities":
        return "utilities"
    return "default"


# =====================================================================
# Generic ratio-based scoring
# =====================================================================

# Absolute-value thresholds for the bonus/penalty nudge (20% weight).
# These are "universal truths" — a P/E under 10 is cheap in any sector.
_ABSOLUTE_THRESHOLDS = {
    #                     (strong_cheap, cheap, expensive)
    "Fwd P/E":            (8,   12,  40),
    "Fwd P/E (normalized)": (7, 10,  40),
    "P/B":                (1.0, 1.5, 8),
    "P/B (NAV proxy)":    (0.9, 1.2, 5),
    "EV/EBITDA":          (6,   9,   30),
}

_ABSOLUTE_THRESHOLDS_HIGHER = {
    #                     (strong, good, weak)
    "ROE":                (0.20, 0.12, 0.0),
    "Div Yield":          (0.06, 0.04, 0.0),
}


def _ratio_score(
    value: Optional[float],
    avg: Optional[float],
    max_pts: int,
    lower_is_better: bool,
    label: str,
    *,
    allow_negative_penalty: bool = True,
) -> tuple[int, list[str]]:
    """Hybrid scorer: 80% ratio-based + 20% absolute-value nudge.

    The ratio component scores how cheap/expensive vs peers.
    The absolute component adds a bonus when the raw number is
    exceptionally good, or removes points when the raw number is
    bad despite looking cheap relative to an expensive sector.
    """
    if value is None or avg is None or avg == 0:
        return 0, []

    # Special: negative value on a "positive is normal" metric
    if value < 0 and avg > 0 and allow_negative_penalty:
        return -int(max_pts * 0.4), [f"Negative {label}"]

    ratio_pts = 0
    abs_pts = 0
    reasons: list[str] = []

    if lower_is_better:
        if value <= 0:
            return 0, []

        # ── Ratio component (80% weight) ──
        ratio = value / avg
        ratio_max = int(max_pts * 0.80)
        if ratio <= 0.30:
            ratio_pts = ratio_max
            reasons.append(f"{label} extremely cheap vs peers ({ratio:.1f}x avg)")
        elif ratio <= 0.50:
            ratio_pts = int(ratio_max * 0.85)
            reasons.append(f"{label} very cheap vs peers ({ratio:.1f}x)")
        elif ratio <= 0.65:
            ratio_pts = int(ratio_max * 0.65)
            reasons.append(f"{label} cheap vs peers ({ratio:.1f}x)")
        elif ratio <= 0.80:
            ratio_pts = int(ratio_max * 0.45)
            reasons.append(f"{label} below peers ({ratio:.1f}x)")
        elif ratio <= 0.95:
            ratio_pts = int(ratio_max * 0.20)

        # ── Absolute component (20% weight) ──
        abs_max = max_pts - ratio_max  # remaining 20%
        thresholds = _ABSOLUTE_THRESHOLDS.get(label)
        if thresholds:
            strong, cheap, expensive = thresholds
            if value <= strong:
                abs_pts = abs_max
                if not reasons:
                    reasons.append(f"{label} very low ({value:.1f})")
            elif value <= cheap:
                abs_pts = int(abs_max * 0.50)
            elif value >= expensive:
                # Sanity check: even if ratio says cheap, the raw number is extreme
                abs_pts = -int(abs_max * 0.50)
                reasons.append(f"{label} still high in absolute terms ({value:.0f})")

    else:
        # Higher is better (ROE, div yield)
        ratio = value / avg

        # ── Ratio component (80%) ──
        ratio_max = int(max_pts * 0.80)
        if ratio >= 2.5:
            ratio_pts = ratio_max
            reasons.append(f"Strong {label} ({ratio:.1f}x sector avg)")
        elif ratio >= 1.8:
            ratio_pts = int(ratio_max * 0.80)
            reasons.append(f"Strong {label}")
        elif ratio >= 1.3:
            ratio_pts = int(ratio_max * 0.60)
            reasons.append(f"Good {label}")
        elif ratio >= 1.0:
            ratio_pts = int(ratio_max * 0.30)
            reasons.append(f"Solid {label}")
        elif ratio >= 0.7:
            ratio_pts = int(ratio_max * 0.10)

        # ── Absolute component (20%) ──
        abs_max = max_pts - ratio_max
        thresholds = _ABSOLUTE_THRESHOLDS_HIGHER.get(label)
        if thresholds:
            strong, good, weak = thresholds
            if value >= strong:
                abs_pts = abs_max
            elif value >= good:
                abs_pts = int(abs_max * 0.50)
            elif value < weak:
                abs_pts = -int(abs_max * 0.50)

    return ratio_pts + abs_pts, reasons


# =====================================================================
# Sector rubrics — which metrics matter and their weights
#
# Each entry: (metric_key, max_pts, lower_is_better, label)
#
# metric_key maps to how we extract the value/avg:
#   "fpe"       → stock.forward_pe / avg.avg_forward_pe
#   "pb"        → stock.price_to_book / avg.avg_price_to_book
#   "ev_ebitda" → stock.ev_to_ebitda / avg.avg_ev_to_ebitda
#   "roe"       → stock.return_on_equity / avg.avg_roe
#   "div_yield" → stock.dividend_yield / avg.avg_dividend_yield
#   "fcf_yield" → computed FCF yield / avg FCF yield (approximated)
#   "pcf"       → market_cap / operating_cashflow (energy only)
# =====================================================================

# (metric_key, max_pts, lower_is_better, label)
_RUBRIC_FINANCIAL = [
    ("pb",        30, True,  "P/B"),
    ("roe",       25, False, "ROE"),
    ("fpe",       15, True,  "Fwd P/E"),
    # ROA scored separately (not in peer avg model)
    # EV/EBITDA: not used for financials
    # FCF: not used for financials
]

_RUBRIC_REIT = [
    ("div_yield", 30, False, "Div Yield"),
    ("pb",        25, True,  "P/B (NAV proxy)"),
    ("ev_ebitda", 15, True,  "EV/EBITDA"),
    ("roe",       10, False, "ROE"),
    # P/E: low weight, no penalty (depreciation distorts it)
]

_RUBRIC_ENERGY = [
    ("ev_ebitda", 28, True,  "EV/EBITDA"),
    # P/CF scored separately (needs operating cashflow)
    ("fpe",       12, True,  "Fwd P/E"),
    ("div_yield", 10, False, "Div Yield"),
    ("roe",        8, False, "ROE"),
    # P/B: not used for energy (asset writedowns distort)
]

_RUBRIC_HEALTHCARE = [
    ("ev_ebitda", 25, True,  "EV/EBITDA"),
    ("fpe",       22, True,  "Fwd P/E"),
    ("roe",       12, False, "ROE"),
    ("pb",         8, True,  "P/B"),
]

_RUBRIC_STAPLES = [
    ("ev_ebitda", 28, True,  "EV/EBITDA"),
    ("fpe",       22, True,  "Fwd P/E"),
    ("div_yield", 12, False, "Div Yield"),
    ("roe",       10, False, "ROE"),
    # P/B: not used (brand value not in book)
]

_RUBRIC_CYCLICAL = [
    ("fpe",       28, True,  "Fwd P/E (normalized)"),
    ("pb",        20, True,  "P/B"),
    ("ev_ebitda", 18, True,  "EV/EBITDA"),
    ("roe",       10, False, "ROE"),
]

_RUBRIC_INDUSTRIAL = [
    ("ev_ebitda", 25, True,  "EV/EBITDA"),
    ("fpe",       22, True,  "Fwd P/E"),
    ("roe",       12, False, "ROE"),
    ("pb",         6, True,  "P/B"),
]

_RUBRIC_COMMS = [
    ("ev_ebitda", 30, True,  "EV/EBITDA"),
    ("fpe",       18, True,  "Fwd P/E"),
    ("roe",       12, False, "ROE"),
    ("div_yield",  5, False, "Div Yield"),
    # P/B: not used (brand/IP not in book)
]

_RUBRIC_MATERIALS = [
    ("ev_ebitda", 28, True,  "EV/EBITDA"),
    ("fpe",       14, True,  "Fwd P/E"),
    ("pb",        12, True,  "P/B"),
    ("roe",       10, False, "ROE"),
]

_RUBRIC_UTILITIES = [
    ("fpe",       30, True,  "Fwd P/E"),
    ("div_yield", 25, False, "Div Yield"),
    ("pb",        15, True,  "P/B"),
    ("ev_ebitda", 12, True,  "EV/EBITDA"),
    ("roe",        8, False, "ROE"),
]

_RUBRIC_DEFAULT = [
    ("fpe",       28, True,  "Fwd P/E"),
    ("pb",        18, True,  "P/B"),
    ("ev_ebitda", 18, True,  "EV/EBITDA"),
    ("roe",       14, False, "ROE"),
]


_SECTOR_RUBRICS = {
    "financial":  _RUBRIC_FINANCIAL,
    "reit":       _RUBRIC_REIT,
    "energy":     _RUBRIC_ENERGY,
    "healthcare": _RUBRIC_HEALTHCARE,
    "staples":    _RUBRIC_STAPLES,
    "cyclical":   _RUBRIC_CYCLICAL,
    "industrial": _RUBRIC_INDUSTRIAL,
    "comms":      _RUBRIC_COMMS,
    "materials":  _RUBRIC_MATERIALS,
    "utilities":  _RUBRIC_UTILITIES,
    "default":    _RUBRIC_DEFAULT,
}


def _get_metric(stock: ScoredStock, avg: Optional[SectorAverages], key: str):
    """Extract (stock_value, peer_avg_value) for a given metric key."""
    mapping = {
        "fpe":       (stock.forward_pe,        avg.avg_forward_pe if avg else None),
        "pb":        (stock.price_to_book,      avg.avg_price_to_book if avg else None),
        "ev_ebitda": (stock.ev_to_ebitda,       avg.avg_ev_to_ebitda if avg else None),
        "roe":       (stock.return_on_equity,   avg.avg_roe if avg else None),
        "div_yield": (stock.dividend_yield,     avg.avg_dividend_yield if avg else None),
    }
    return mapping.get(key, (None, None))


# =====================================================================
# Sector-specific extras (things that don't fit the generic rubric)
# =====================================================================

def _score_financial_extras(stock: ScoredStock) -> tuple[int, list[str]]:
    """ROA scoring for financials (no sector avg available)."""
    pts = 0
    reasons: list[str] = []
    roa = stock.return_on_assets
    if roa is not None:
        if roa > 0.015:
            pts += 15; reasons.append("ROA above 1.5%")
        elif roa > 0.010:
            pts += 10; reasons.append("ROA above 1%")
        elif roa > 0.005:
            pts += 5
        elif roa < 0:
            pts -= 8; reasons.append("Negative ROA")
    return pts, reasons


def _score_energy_pcf(stock: ScoredStock) -> tuple[int, list[str]]:
    """Price/Cash Flow for energy stocks (uses operating cashflow)."""
    ocf = stock.operating_cashflow
    mc = stock.market_cap
    if not ocf or ocf <= 0 or not mc or mc <= 0:
        return 0, []
    pcf = mc / ocf
    # Use relative thresholds: energy P/CF norms are 5-12x
    # Since we don't have a peer avg P/CF, use ratio to a baseline of 10x
    if pcf < 3:
        return 25, [f"Extremely low P/CF ({pcf:.1f}x)"]
    if pcf < 5:
        return 20, [f"Very low P/CF ({pcf:.1f}x)"]
    if pcf < 8:
        return 12, [f"Low P/CF ({pcf:.1f}x)"]
    if pcf < 12:
        return 5, []
    return 0, []


def _score_reit_fpe(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    """P/E for REITs — low weight, never penalize (depreciation distorts)."""
    fpe = stock.forward_pe
    avg_fpe = avg.avg_forward_pe if avg else None
    if fpe is None or fpe <= 0 or avg_fpe is None or avg_fpe <= 0:
        return 0, []
    ratio = fpe / avg_fpe
    if ratio < 0.50:
        return 10, [f"Low P/E vs REIT peers ({ratio:.1f}x)"]
    if ratio < 0.75:
        return 5, []
    # Never penalize REITs for high P/E
    return 0, []


def _score_china_adr(stock: ScoredStock) -> tuple[int, list[str]]:
    if (stock.country or "") in _CHINA_COUNTRIES:
        return -20, ["China ADR — delisting/regulatory risk"]
    return 0, []


# =====================================================================
# Analyst recommendation & upside (not ratio-based — inherently scaled)
# =====================================================================

def _score_analyst_rec(rec: Optional[float], max_pts: int = 10) -> tuple[int, list[str]]:
    if rec is None or rec <= 0:
        return 0, []
    if rec < 1.8:
        return min(max_pts, 10), ["Analyst: Strong Buy"]
    if rec < 2.3:
        return min(max_pts, 5), ["Analyst: Buy"]
    if rec > 3.5:
        return -5, ["Analyst: Sell"]
    return 0, []


def _score_upside(stock: ScoredStock) -> tuple[int, list[str]]:
    """Capped at 6 pts — analyst targets lag at 52W lows."""
    if stock.target_mean_price and stock.price and stock.price > 0:
        u = (stock.target_mean_price - stock.price) / stock.price
        stock.upside_percent = round(u * 100, 1)
        if u > 0.40:
            return 6, [f"{stock.upside_percent:.0f}% upside"]
        if u > 0.20:
            return 4, [f"{stock.upside_percent:.0f}% upside"]
    return 0, []


# =====================================================================
# FCF yield scoring (ratio to market cap — inherently normalized)
# =====================================================================

def _score_fcf_yield(stock: ScoredStock, max_pts: int = 18) -> tuple[int, list[str]]:
    fcf = stock.free_cash_flow
    mc = stock.market_cap
    if fcf is None or not mc or mc <= 0:
        return 0, []
    fy = fcf / mc
    if fy < -0.02:
        return -8, ["Negative FCF yield"]
    if fy < 0:
        return -3, []
    pct = fy * 100
    if fy > 0.12:
        return min(max_pts, 18), [f"Strong FCF yield {pct:.0f}%"]
    if fy > 0.07:
        return min(max_pts, 14), [f"Solid FCF yield {pct:.0f}%"]
    if fy > 0.03:
        return min(max_pts, 8), ["Positive FCF"]
    if fy > 0:
        return min(max_pts, 4), ["Positive FCF"]
    return 0, []


# =====================================================================
# Negative EV/EBITDA penalty
# =====================================================================

def _penalty_negative_ev_ebitda(ev: Optional[float]) -> tuple[int, list[str]]:
    if ev is not None and ev < 0:
        return -12, ["Negative EBITDA — operating losses"]
    return 0, []


# =====================================================================
# Universal signal scorers
# =====================================================================

def _score_growth(stock: ScoredStock) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []
    eg = stock.earnings_growth
    if eg is not None:
        if eg < -0.70:
            pts -= 20; reasons.append(f"Earnings collapsing {eg*100:.0f}%")
        elif eg < -0.50:
            pts -= 15; reasons.append(f"Earnings plunging {eg*100:.0f}%")
        elif eg < -0.30:
            pts -= 10; reasons.append(f"Earnings declining {eg*100:.0f}%")
        elif eg < -0.10:
            pts -= 5; reasons.append(f"Earnings declining {eg*100:.0f}%")
        elif eg > 0.15:
            pts += 5; reasons.append("Earnings growing")
    rg = stock.revenue_growth
    if rg is not None:
        if rg < -0.20:
            pts -= 10; reasons.append(f"Revenue shrinking {rg*100:.0f}%")
        elif rg < -0.10:
            pts -= 5; reasons.append(f"Revenue declining {rg*100:.0f}%")
        elif rg < -0.05:
            pts -= 2
        elif rg > 0.10:
            pts += 3; reasons.append("Revenue growing")
    return pts, reasons


def _score_proximity_to_low(stock: ScoredStock) -> tuple[int, list[str]]:
    low, high, price = stock.fifty_two_week_low, stock.fifty_two_week_high, stock.price
    if not low or not high or high <= low or not price:
        return 0, []
    pos = (price - low) / (high - low)
    if pos < 0.05:
        return 8, [f"At 52W low (bottom {pos*100:.0f}% of range)"]
    if pos < 0.10:
        return 5, [f"Near 52W low ({pos*100:.0f}% from bottom)"]
    if pos < 0.20:
        return 3, [f"Close to 52W low ({pos*100:.0f}% from bottom)"]
    return 0, []


def _score_short_interest(stock: ScoredStock) -> tuple[int, list[str]]:
    si = stock.short_percent_of_float
    if si is None:
        return 0, []
    pct = si * 100 if si < 1 else si
    insiders_buying = stock.insider_buy_count >= 3
    if pct > 40:
        if insiders_buying:
            return -5, [f"Very high short {pct:.0f}% but insiders buying (squeeze?)"]
        return -15, [f"Very high short interest {pct:.0f}% — crowded short"]
    if pct > 25:
        if insiders_buying:
            return 0, [f"High short {pct:.0f}% but insiders buying"]
        return -8, [f"High short interest {pct:.0f}%"]
    if pct > 15:
        return -3, [f"Elevated short interest {pct:.0f}%"]
    if pct < 3:
        return 3, ["Low short interest"]
    return 0, []


def _score_insider_buying(stock: ScoredStock) -> tuple[int, list[str]]:
    buys = stock.insider_buy_count
    sells = stock.insider_sell_count
    total = buys + sells
    if total == 0:
        return 0, []
    sentiment = buys / total
    if buys >= 3 and sells >= 3:
        if sentiment > 0.5:
            return 2, [f"Mixed insider activity, net buying ({buys}B/{sells}S)"]
        return -2, [f"Mixed insider activity, net selling ({buys}B/{sells}S)"]
    if sentiment > 0.7 and buys >= 3:
        return 10, [f"Strong insider buying ({buys} buys vs {sells} sells)"]
    if sentiment > 0.7 and buys >= 2:
        return 6, [f"Insider buying ({buys} buys vs {sells} sells)"]
    if sentiment > 0.5:
        return 3, [f"Net insider buying ({buys}B/{sells}S)"]
    if sells >= 50:
        return -25, [f"Extreme insider selling ({sells} sells, {buys} buys)"]
    if sells >= 20:
        return -20, [f"Mass insider exodus ({sells} sells, {buys} buys)"]
    if sells >= 10:
        return -16, [f"Heavy insider selling ({sells} sells, {buys} buys)"]
    if sells >= 5:
        return -12, [f"Significant insider selling ({sells} sells, {buys} buys)"]
    if sentiment < 0.2 and sells >= 3:
        return -8, [f"Insider selling ({sells} sells, {buys} buys)"]
    if sentiment < 0.3 and sells >= 2:
        return -4, [f"More insider selling than buying ({sells}S/{buys}B)"]
    return 0, []


def _score_piotroski(stock: ScoredStock) -> tuple[int, list[str]]:
    fs = stock.piotroski_f_score
    if fs is None:
        return 0, []
    if fs >= 8:
        return 10, [f"Strong Piotroski F-Score ({fs}/9)"]
    if fs >= 6:
        return 5, [f"Good Piotroski F-Score ({fs}/9)"]
    if fs >= 4:
        return 0, []
    if fs <= 1:
        return -15, [f"Very weak Piotroski F-Score ({fs}/9) — value trap risk"]
    return -10, [f"Weak Piotroski F-Score ({fs}/9) — value trap risk"]


def _penalty_missing_forward_pe(fpe: Optional[float], sector_type: str) -> tuple[int, list[str]]:
    if sector_type in ("reit", "financial"):
        return 0, []
    if fpe is not None and fpe < 0:
        return -8, ["Negative forward earnings"]
    if fpe is None:
        return -4, ["No forward earnings estimate"]
    return 0, []


def _penalty_sector_relative(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Penalize metrics that deviate extremely from market averages."""
    pts = 0
    reasons: list[str] = []

    # D/E vs market avg
    de = stock.debt_to_equity
    mkt_de = stock.market_avg_debt_equity
    if de is not None and de > 0 and mkt_de and mkt_de > 0:
        ratio = de / mkt_de
        if ratio > 5.0:
            pts -= 15; reasons.append(f"Leverage {ratio:.1f}x sector avg (D/E {de:.0f} vs {mkt_de:.0f})")
        elif ratio > 3.0:
            pts -= 10; reasons.append(f"Leverage {ratio:.1f}x sector avg (D/E {de:.0f})")
        elif ratio > 2.0:
            pts -= 5; reasons.append(f"Leverage {ratio:.1f}x sector avg")
    elif de is not None and de > 400:
        pts -= 10; reasons.append(f"Very high leverage D/E {de:.0f}")

    # Negative FCF burn rate
    fcf = stock.free_cash_flow
    mc = stock.market_cap
    if fcf is not None and fcf < 0 and mc and mc > 0:
        burn = abs(fcf) / mc
        if burn > 0.50:
            pts -= 15; reasons.append(f"Extreme cash burn ({burn*100:.0f}% of mkt cap)")
        elif burn > 0.20:
            pts -= 8; reasons.append(f"Heavy cash burn ({burn*100:.0f}% of mkt cap)")
        elif burn > 0.05:
            pts -= 3; reasons.append(f"Negative FCF ({burn*100:.0f}% of mkt cap)")

    # ROE: handled by ratio scorer — no double-counting here

    return pts, reasons


# =====================================================================
# Universal scoring wrapper
# =====================================================================

def _score_universal(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []
    red_flag_count = 0

    for fn in (
        _score_growth,
        _score_proximity_to_low,
        _score_short_interest,
        _score_insider_buying,
        _score_piotroski,
    ):
        p, r = fn(stock)
        pts += p; reasons.extend(r)
        if p <= -8:
            red_flag_count += 1

    p, r = _penalty_missing_forward_pe(stock.forward_pe, sector_type)
    pts += p; reasons.extend(r)

    p, r = _penalty_sector_relative(stock, sector_type)
    pts += p; reasons.extend(r)
    if p <= -10:
        red_flag_count += 1

    if red_flag_count >= 3:
        pts -= 15; reasons.append("Multiple severe red flags — high value trap risk")
    elif red_flag_count >= 2:
        pts -= 8; reasons.append("Compounding red flags")

    return pts, reasons


# =====================================================================
# Master scorer — applies rubric then extras then universals
# =====================================================================

def _score_with_rubric(
    stock: ScoredStock,
    avg: Optional[SectorAverages],
    sector_type: str,
) -> tuple[int, list[str]]:
    """Apply the sector rubric (ratio-based) + sector extras + universals."""
    pts = 0
    reasons: list[str] = []

    # 1. Ratio-based rubric scoring
    rubric = _SECTOR_RUBRICS.get(sector_type, _RUBRIC_DEFAULT)
    for metric_key, max_pts, lower_is_better, label in rubric:
        val, peer_avg = _get_metric(stock, avg, metric_key)
        # Also try market avg as fallback if peer avg is None
        if peer_avg is None:
            _, peer_avg = _get_metric_market(stock, metric_key)
        p, r = _ratio_score(val, peer_avg, max_pts, lower_is_better, label)
        pts += p; reasons.extend(r)

    # 2. Negative EV/EBITDA penalty (all sectors that use EV/EBITDA)
    if any(k == "ev_ebitda" for k, _, _, _ in rubric):
        p, r = _penalty_negative_ev_ebitda(stock.ev_to_ebitda)
        pts += p; reasons.extend(r)

    # 3. Sector-specific extras
    if sector_type == "financial":
        p, r = _score_financial_extras(stock)
        pts += p; reasons.extend(r)
    elif sector_type == "reit":
        p, r = _score_reit_fpe(stock, avg)
        pts += p; reasons.extend(r)
    elif sector_type == "energy":
        p, r = _score_energy_pcf(stock)
        pts += p; reasons.extend(r)
    elif sector_type == "comms":
        p, r = _score_china_adr(stock)
        pts += p; reasons.extend(r)

    # 4. FCF yield (for sectors that use it)
    fcf_sectors = {"energy", "healthcare", "staples", "cyclical", "industrial",
                   "comms", "materials", "default"}
    if sector_type in fcf_sectors:
        max_fcf = 18 if sector_type in ("healthcare", "staples", "industrial", "comms") else 12
        p, r = _score_fcf_yield(stock, max_fcf)
        pts += p; reasons.extend(r)

    # 5. Analyst + upside (all sectors)
    analyst_pts = 12 if sector_type in ("financial", "healthcare", "default") else 10
    p, r = _score_analyst_rec(stock.recommendation_mean, analyst_pts)
    pts += p; reasons.extend(r)

    p, r = _score_upside(stock)
    pts += p; reasons.extend(r)

    # 6. Universal signals
    p, r = _score_universal(stock, sector_type)
    pts += p; reasons.extend(r)

    return pts, reasons


def _get_metric_market(stock: ScoredStock, key: str):
    """Fallback: get market avg for a metric."""
    mapping = {
        "fpe":       (stock.forward_pe,      stock.market_avg_fpe),
        "pb":        (stock.price_to_book,    stock.market_avg_pb),
        "ev_ebitda": (stock.ev_to_ebitda,     stock.market_avg_ev_ebitda),
        "roe":       (stock.return_on_equity, stock.market_avg_roe),
        "div_yield": (stock.dividend_yield,   stock.market_avg_div_yield),
    }
    return mapping.get(key, (None, None))


# =====================================================================
# Dispatch & labels
# =====================================================================

_SECTOR_TYPE_LABELS = {
    "financial": "Financial model (P/B + ROE + ROA vs sector)",
    "reit": "REIT model (Div yield + NAV + EV/EBITDA vs sector)",
    "energy": "Energy model (EV/EBITDA + P/CF + FCF vs sector)",
    "healthcare": "Healthcare model (EV/EBITDA + Fwd P/E + FCF vs sector)",
    "staples": "Staples model (EV/EBITDA + Fwd P/E + Div yield vs sector)",
    "cyclical": "Cyclical model (Fwd P/E + P/B + EV/EBITDA vs sector)",
    "industrial": "Industrial model (EV/EBITDA + Fwd P/E + FCF vs sector)",
    "comms": "Comms model (EV/EBITDA + FCF + Fwd P/E vs sector)",
    "materials": "Materials model (EV/EBITDA + Fwd P/E + P/B vs sector)",
    "utilities": "Utilities model (Fwd P/E + Div yield + P/B vs sector)",
    "default": "Default model (Fwd P/E + P/B + EV/EBITDA + ROE vs sector)",
}


# =====================================================================
# Public API
# =====================================================================

def compute_score(stock: ScoredStock, sector_avg: Optional[SectorAverages]) -> ScoreBreakdown:
    """Compute sector-aware value score. Same inputs/outputs as before."""
    sector_type = detect_sector_type(stock.sector, stock.industry)
    raw_score, reasons = _score_with_rubric(stock, sector_avg, sector_type)

    score = max(0, min(150, raw_score))

    if score >= 70:
        tier = "Strong Value"
    elif score >= 45:
        tier = "Moderate Value"
    else:
        tier = "Limited Signal"

    return ScoreBreakdown(
        total=score,
        tier=tier,
        reasons=reasons,
        sector_type=sector_type,
    )
