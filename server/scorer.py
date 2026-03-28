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
    # EV/EBITDA replaced by P/FFO (scored separately in extras)
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
    ("pb",         8, True,  "P/B"),
    ("ev_ebitda", 18, True,  "EV/EBITDA"),
    ("roe",       14, False, "ROE"),
    # EV/Gross Profit scored separately in extras (10 pts)
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
    from server.config import CHINA_ADR_PENALTY
    if (stock.country or "") in _CHINA_COUNTRIES:
        if CHINA_ADR_PENALTY == 0:
            return 0, []
        return CHINA_ADR_PENALTY, [f"China ADR regulatory discount ({CHINA_ADR_PENALTY} pts)"]
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
    """Change 7: Increased upside cap from 6 to 10 pts."""
    if stock.target_mean_price and stock.price and stock.price > 0:
        u = (stock.target_mean_price - stock.price) / stock.price
        stock.upside_percent = round(u * 100, 1)
        if u > 0.60:
            return 10, [f"{stock.upside_percent:.0f}%+ analyst upside to target"]
        if u > 0.40:
            return 8, [f"{stock.upside_percent:.0f}%+ analyst upside to target"]
        if u > 0.25:
            return 5, [f"{stock.upside_percent:.0f}%+ analyst upside"]
        if u > 0.10:
            return 3, []
        if u < 0:
            return -5, ["Analyst consensus below current price"]
    return 0, []


# =====================================================================
# Change 1: FCF yield on EV + sector-relative bonus
# =====================================================================

def _score_fcf_yield(stock: ScoredStock, max_pts: int = 18) -> tuple[int, list[str]]:
    fcf = stock.free_cash_flow
    ev = stock.enterprise_value
    denom = ev if ev and ev > 0 else stock.market_cap
    if fcf is None or not denom or denom <= 0:
        return 0, []
    fy = fcf / denom
    if fy < -0.02:
        return -8, ["Negative FCF yield"]
    if fy < 0:
        return -3, []
    pct = fy * 100
    pts = 0
    reasons: list[str] = []
    if fy > 0.12:
        pts = min(max_pts, 18); reasons.append(f"Strong FCF/EV yield {pct:.0f}%")
    elif fy > 0.07:
        pts = min(max_pts, 14); reasons.append(f"Solid FCF/EV yield {pct:.0f}%")
    elif fy > 0.03:
        pts = min(max_pts, 8); reasons.append("Positive FCF")
    elif fy > 0:
        pts = min(max_pts, 4); reasons.append("Positive FCF")
    # Sector-relative FCF yield bonus (Change 1 addition)
    # Compare to a rough sector FCF yield if we have market avg data
    mkt_ev_ebitda = stock.market_avg_ev_ebitda
    if mkt_ev_ebitda and mkt_ev_ebitda > 0 and fy > 0:
        # Approximate sector FCF yield as ~60% of 1/EV_EBITDA (rough FCF proxy)
        sector_fy = 0.6 / mkt_ev_ebitda
        if fy > sector_fy * 1.5:
            pts += 4; reasons.append("FCF yield well above sector")
    return pts, reasons


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
    """Changes 6, 8: Enhanced revenue tiers + EBITDA cross-check on collapse."""
    pts = 0
    reasons: list[str] = []
    eg = stock.earnings_growth
    rg = stock.revenue_growth
    ebitda_g = stock.ebitda_growth

    # --- Earnings growth with Change 8: EBITDA cross-check ---
    if eg is not None:
        raw_penalty = 0
        if eg < -0.70:
            raw_penalty = -20; reasons.append(f"Earnings collapsing {eg*100:.0f}%")
        elif eg < -0.50:
            raw_penalty = -15; reasons.append(f"Earnings plunging {eg*100:.0f}%")
        elif eg < -0.30:
            raw_penalty = -10; reasons.append(f"Earnings declining {eg*100:.0f}%")
        elif eg < -0.10:
            raw_penalty = -5; reasons.append(f"Earnings declining {eg*100:.0f}%")
        elif eg > 0.15:
            pts += 5; reasons.append("Earnings growing")

        # Change 8: EBITDA cross-check on collapse
        if raw_penalty <= -10 and ebitda_g is not None:
            if eg < -0.50 and ebitda_g > -0.20:
                # GAAP earnings collapse but EBITDA is okay — likely one-time items
                raw_penalty = int(raw_penalty * 0.50)
                reasons.append("GAAP earnings decline may include one-time items")
            elif rg is not None and rg > 0 and eg < -0.30:
                # Earnings declining but revenue growing — margin compression
                raw_penalty = int(raw_penalty * 0.70)
                if reasons and "collaps" in reasons[-1].lower():
                    reasons[-1] = f"Margin compression {eg*100:.0f}%"
        # Double confirmation: both earnings AND revenue negative → full penalty
        if raw_penalty < 0 and rg is not None and rg < 0 and eg < -0.30:
            pass  # keep full penalty, no reduction

        pts += raw_penalty

    # --- Change 6: Enhanced revenue growth tiers ---
    if rg is not None:
        if rg < -0.15:
            pts -= 15; reasons.append(f"Revenue collapsing {rg*100:.0f}%")
        elif rg < -0.05:
            pts -= 8; reasons.append(f"Revenue declining {rg*100:.0f}%")
        elif rg < 0:
            pts -= 3
        elif rg > 0.15:
            pts += 5; reasons.append("Strong revenue growth")
        elif rg > 0.08:
            pts += 4; reasons.append("Solid revenue growth")
        elif rg > 0.03:
            pts += 3; reasons.append("Positive revenue growth")
        elif rg > 0:
            pts += 2; reasons.append("Modest but positive growth")

    # Revenue acceleration bonus (Change 6)
    ra = stock.revenue_acceleration
    if ra is not None and ra > 0.05:
        pts += 4; reasons.append("Revenue accelerating")

    # Relative momentum (Change 5 — enhanced tiers)
    rm = stock.relative_momentum
    if rm is not None:
        if rm > 15:
            pts += 8; reasons.append(f"Strong positive relative momentum (+{rm:.0f}pp vs sector)")
        elif rm > 5:
            pts += 5; reasons.append(f"Outperforming sector peers (+{rm:.0f}pp)")
        elif rm > 0:
            pts += 3
        elif rm < -20:
            pts -= 5; reasons.append(f"Underperforming sector peers badly ({rm:.0f}pp)")
        elif rm < -8:
            pts -= 2

    return pts, reasons


def _score_proximity_to_low(stock: ScoredStock) -> tuple[int, list[str]]:
    """Change 12: Proximity as interaction signal only (with F-Score + FCF)."""
    low, high, price = stock.fifty_two_week_low, stock.fifty_two_week_high, stock.price
    if not low or not high or high <= low or not price:
        return 0, []
    pos = (price - low) / (high - low)
    fs = stock.piotroski_f_score

    # FCF yield for interaction check
    ev = stock.enterprise_value
    denom = ev if ev and ev > 0 else stock.market_cap
    fcf_yield = stock.free_cash_flow / denom if stock.free_cash_flow and denom and denom > 0 else 0

    if pos < 0.05:
        if fs is not None and fs >= 7:
            return 6, ["Deeply oversold with strong fundamentals — potential capitulation bottom"]
        if fs is not None and fs <= 3:
            return -5, ["At 52W low with weak fundamentals — possible free fall"]
    elif pos < 0.20:
        if fs is not None and fs >= 7 and fcf_yield > 0.05:
            return 4, ["Slight recovery from low with strong quality + FCF"]

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


def _score_accruals_quality(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Change A: Sloan accruals quality ratio for Value Score."""
    if sector_type in ("financial", "reit"):
        return 0, []
    ar = stock.accruals_ratio
    if ar is None:
        return 0, []
    if ar < -0.10:
        return 8, [f"High earnings quality: cash earnings well above reported (accruals {ar*100:.0f}%)"]
    if ar < -0.05:
        return 5, ["Good earnings quality: cash exceeds reported earnings"]
    if ar < 0:
        return 2, []
    if ar <= 0.05:
        return 0, []
    if ar <= 0.10:
        return -5, ["Moderate accruals: reported earnings above cash flow"]
    if ar <= 0.20:
        return -10, ["High accruals: earnings quality concern"]
    return -15, ["Very high accruals: significant earnings quality risk"]


def _score_accruals_quality_q(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Change A: Accruals quality for Quality Score."""
    if sector_type in ("financial", "reit"):
        return 0, []
    ar = stock.accruals_ratio
    if ar is None:
        return 0, []
    if ar < -0.05:
        return 6, ["Cash earnings exceed reported — quality"]
    if ar > 0.20:
        return -12, ["Severe accruals — possible earnings manipulation"]
    if ar > 0.10:
        return -8, ["Reported earnings materially above cash — earnings quality risk"]
    return 0, []


def _score_eps_revision(stock: ScoredStock) -> tuple[int, list[str]]:
    """Change B: Earnings estimate revision momentum for Quality Score."""
    pts = 0
    reasons: list[str] = []

    # Primary: EPS surprise trend
    st = stock.eps_surprise_trend
    used_primary = False
    if st is not None:
        used_primary = True
        if st > 0.10:
            pts += 8; reasons.append("EPS surprises improving — estimate revision tailwind")
        elif st > 0.03:
            pts += 4; reasons.append("EPS surprises stable-to-improving")
        elif st < -0.10:
            pts -= 8; reasons.append("EPS surprises deteriorating — estimate cuts likely")
        elif st < -0.03:
            pts -= 4; reasons.append("EPS surprise trend weakening")

    # Secondary: earningsGrowth if no surprise trend
    if not used_primary:
        eg = stock.earnings_growth
        if eg is not None:
            if eg > 0.20:
                pts += 5; reasons.append("Strong earnings growth momentum")
            elif eg > 0.05:
                pts += 3
            elif eg < -0.20:
                pts -= 5; reasons.append("Earnings contracting sharply")
            elif eg < -0.05:
                pts -= 2

    # Independent: average EPS surprise
    avg = stock.avg_eps_surprise
    if avg is not None:
        if avg > 0.05:
            pts += 4; reasons.append("Consistent EPS beats — management guides conservatively")
        elif avg < -0.05:
            pts -= 4; reasons.append("Consistent EPS misses — guidance credibility concern")

    # Interaction: positive revision at 52W low
    if pts > 0 and stock.fifty_two_week_high and stock.price and stock.fifty_two_week_high > 0:
        pos = (stock.price - stock.fifty_two_week_low) / (stock.fifty_two_week_high - stock.fifty_two_week_low) if stock.fifty_two_week_high > stock.fifty_two_week_low else 1
        if pos < 0.10:
            pts += 5; reasons.append("Improving estimates at 52W low — high-conviction contrarian setup")

    return pts, reasons


def _score_historical_mean_reversion(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Change C: Historical multiple mean reversion signal for Value Score."""
    if sector_type == "energy":
        return 0, []  # commodity earnings too volatile

    pts = 0
    reasons: list[str] = []

    # Dividend yield vs 5-year average
    dy = stock.dividend_yield
    avg_dy = stock.five_year_avg_div_yield
    if dy and dy > 0.01 and avg_dy and avg_dy > 0:
        ratio = dy / avg_dy
        dy_pts = 0
        if ratio > 1.5:
            dy_pts = 8; reasons.append(f"Yielding {ratio:.0f}x above 5-year avg — historically cheap")
        elif ratio > 1.25:
            dy_pts = 5; reasons.append("Yield above 5-year average")
        elif ratio > 1.0:
            dy_pts = 2
        elif ratio < 0.75:
            dy_pts = -5; reasons.append("Yield well below historical average")
        # Double for utilities
        if sector_type == "utility":
            dy_pts = int(dy_pts * 2)
        pts += dy_pts

    # Forward P/E recovery signal (skip for financial/reit)
    if sector_type not in ("financial", "reit"):
        fpe = stock.forward_pe
        tpe = stock.trailing_pe
        if fpe and fpe > 0 and fpe < 200 and tpe and tpe > 0 and tpe < 200:
            recovery = fpe / tpe
            if recovery < 0.60:
                pts += 8; reasons.append("Forward P/E 40%+ below trailing — earnings recovery priced in")
            elif recovery < 0.75:
                pts += 5; reasons.append("Forward P/E well below trailing — improving earnings expected")
            elif recovery < 0.85:
                pts += 2
            elif recovery > 1.50:
                pts -= 10; reasons.append("Significant earnings deterioration expected")
            elif recovery > 1.20:
                pts -= 5; reasons.append("Forward P/E above trailing — earnings expected to decline")

    # Cap at +12 to prevent double-counting
    pts = max(-15, min(12, pts))
    return pts, reasons


def _score_insider_buying(stock: ScoredStock) -> tuple[int, list[str]]:
    """Change 2: Sell penalties reduced ~40% (most sells are pre-planned 10b5-1)."""
    buys = stock.insider_buy_count
    sells = stock.insider_sell_count
    total = buys + sells
    if total == 0:
        return 0, []
    sentiment = buys / total
    # Buy signals unchanged
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
    # Sell penalties reduced ~40% from original
    if sells >= 50:
        return -15, [f"Extreme insider selling ({sells} sells, {buys} buys)"]
    if sells >= 20:
        return -10, [f"Mass insider selling ({sells} sells, {buys} buys)"]
    if sells >= 5:
        return -5, [f"Insider selling ({sells} sells, {buys} buys)"]
    if sentiment < 0.2 and sells >= 3:
        return -3, [f"Insider selling ({sells} sells, {buys} buys)"]
    if sentiment < 0.3 and sells >= 2:
        return -2, [f"More insider selling than buying ({sells}S/{buys}B)"]
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


def _penalty_leverage(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Change 10: Net Debt/EBITDA replaces D/E for leverage assessment."""
    if sector_type in ("utility", "reit"):
        return 0, []  # high leverage structurally normal

    pts = 0
    reasons: list[str] = []
    nde = stock.net_debt_ebitda

    if nde is not None:
        if nde < -0.5:
            pts += 8; reasons.append("Significant net cash — fortress balance sheet")
        elif nde < 0:
            pts += 5; reasons.append("Net cash position")
        elif nde <= 2.5:
            pass  # healthy
        elif nde <= 4.0:
            pts -= 3; reasons.append(f"Elevated leverage ({nde:.1f}x Net Debt/EBITDA)")
        elif nde <= 6.0:
            pts -= 10; reasons.append(f"High leverage: {nde:.1f}x Net Debt/EBITDA")
        else:
            pts -= 15; reasons.append(f"Extreme leverage: {nde:.1f}x Net Debt/EBITDA")
    else:
        # Fallback: check D/E vs sector avg (old approach)
        de = stock.debt_to_equity
        mkt_de = stock.market_avg_debt_equity
        if de is not None and de > 0 and mkt_de and mkt_de > 0:
            ratio = de / mkt_de
            if ratio > 5.0:
                pts -= 12; reasons.append(f"Leverage {ratio:.1f}x sector avg (D/E {de:.0f})")
            elif ratio > 3.0:
                pts -= 8; reasons.append(f"Leverage {ratio:.1f}x sector avg")
            elif ratio > 2.0:
                pts -= 4; reasons.append(f"Elevated leverage vs sector")

    # Negative FCF burn rate (kept from old _penalty_sector_relative)
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

    return pts, reasons


def _score_altman_z(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Change 9: Altman Z-Score distress flag."""
    if sector_type in ("financial", "reit", "utility"):
        return 0, []
    z = stock.altman_z_score
    if z is None:
        return 0, []
    if z < 1.81:
        return -20, [f"Bankruptcy risk: Altman Z-Score {z:.1f} (distress zone)"]
    if z < 2.99:
        return -8, [f"Altman Z-Score {z:.1f} (grey zone — elevated risk)"]
    if z > 4.0:
        return 5, [f"Strong Altman Z-Score {z:.1f} — low distress risk"]
    return 0, []


def _score_interest_coverage(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Change 13: Interest coverage ratio penalty/bonus."""
    if sector_type in ("financial", "reit"):
        return 0, []
    ic = stock.interest_coverage
    if ic is None:
        return 0, []
    pts = 0
    reasons: list[str] = []
    if ic < 1.0:
        pts -= 18; reasons.append("EBIT does not cover interest — acute distress risk")
    elif ic < 1.5:
        pts -= 10; reasons.append("Thin interest coverage — one bad quarter from distress")
    elif ic < 2.5:
        pts -= 4; reasons.append("Below-average interest coverage")
    elif ic < 5.0:
        pass  # acceptable
    elif ic < 10.0:
        pts += 4; reasons.append("Strong interest coverage")
    else:
        pts += 8; reasons.append("Excellent debt service capacity")

    # Compounding: high leverage + thin coverage
    nde = stock.net_debt_ebitda
    if nde is not None and nde > 4.0 and ic < 2.0:
        pts -= 10; reasons.append("High leverage + thin coverage = significant distress risk")

    return pts, reasons


def _score_dividend_sustainability(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Change 11: Dividend payout ratio sustainability check."""
    if sector_type not in ("reit", "utility", "staples", "comms"):
        return 0, []
    pr = stock.payout_ratio
    if pr is None or pr < 0:
        return 0, []
    pts = 0
    reasons: list[str] = []
    if pr > 1.5:
        pts -= 15; reasons.append("Dividend likely unsustainable — payout exceeds earnings by 50%")
    elif pr > 1.0:
        pts -= 8; reasons.append("Dividend payout ratio above 100% — at risk of cut")
    elif pr > 0.85:
        pts -= 3; reasons.append("Payout ratio stretched")
    elif pr < 0.4:
        pts += 4; reasons.append("Conservative payout ratio — dividend well covered")

    # Override: if FCF covers dividend even when GAAP doesn't
    if pts < 0 and stock.free_cash_flow and stock.dividend_yield and stock.market_cap:
        div_cost = stock.dividend_yield * stock.market_cap
        if stock.free_cash_flow > div_cost:
            pts = int(pts * 0.50)
            reasons.append("FCF covers dividend despite high payout ratio")

    return pts, reasons


# =====================================================================
# New batch scoring functions
# =====================================================================

def _score_gp_to_assets(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Novy-Marx (2013) gross profitability: GP / Total Assets."""
    if sector_type in ("financial", "reit"):
        return 0, []
    gpa = stock.gp_to_assets
    if gpa is None:
        return 0, []
    if gpa > 0.40:
        return 12, [f"High gross profitability (GP/A {gpa*100:.0f}%)"]
    if gpa > 0.25:
        return 8, [f"Good gross profitability (GP/A {gpa*100:.0f}%)"]
    if gpa > 0.15:
        return 4, []
    if gpa > 0.05:
        return 2, []
    if gpa <= 0:
        return -5, ["Negative gross profitability"]
    return 0, []


def _score_roic_vs_wacc(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """ROIC vs sector WACC spread."""
    if sector_type in ("financial", "reit"):
        return 0, []
    roic = stock.roic
    if roic is None:
        return 0, []
    from server.damodaran_benchmarks import SECTOR_WACC
    wacc = SECTOR_WACC.get(stock.sector, 0.085)
    if roic > wacc * 2:
        return 8, [f"ROIC {roic*100:.0f}% > 2x WACC ({wacc*100:.0f}%) — exceptional value creation"]
    if roic > wacc * 1.5:
        return 6, [f"ROIC {roic*100:.0f}% > 1.5x WACC"]
    if roic > wacc:
        return 3, [f"ROIC above sector WACC"]
    if roic > 0:
        return -2, ["ROIC below sector WACC"]
    return -6, ["Negative ROIC — destroying value"]


def _score_p_ffo(stock: ScoredStock) -> tuple[int, list[str]]:
    """P/FFO for REITs (replaces EV/EBITDA in REIT model)."""
    pffo = stock.p_ffo
    if pffo is None or pffo <= 0:
        return 0, []
    if pffo < 8:
        return 15, [f"Very low P/FFO ({pffo:.1f}x)"]
    if pffo < 12:
        return 12, [f"Low P/FFO ({pffo:.1f}x)"]
    if pffo < 16:
        return 8, [f"Reasonable P/FFO ({pffo:.1f}x)"]
    if pffo < 20:
        return 4, []
    if pffo <= 28:
        return 0, []
    return -5, [f"Expensive P/FFO ({pffo:.1f}x)"]


def _score_beneish(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Beneish M-Score earnings manipulation flag."""
    if sector_type in ("financial", "reit"):
        return 0, []
    m = stock.beneish_m_score
    if m is None:
        return 0, []
    if m > -1.78:
        return -10, [f"Beneish M-Score {m:.1f} — possible earnings manipulation"]
    if m > -2.22:
        return -5, [f"Beneish M-Score {m:.1f} — elevated manipulation risk"]
    return 0, []


def _score_asset_growth(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Asset growth penalty (Cooper et al. 2008 asset growth anomaly)."""
    if sector_type == "reit":
        return 0, []
    ag = stock.asset_growth
    if ag is None:
        return 0, []
    pts = 0
    reasons: list[str] = []
    if ag > 0.30:
        pts = -5; reasons.append(f"Rapid asset growth {ag*100:.0f}% — potential overinvestment")
    elif ag > 0.20:
        pts = -3; reasons.append(f"High asset growth {ag*100:.0f}%")
    elif ag < -0.10:
        pts = 3; reasons.append("Asset base shrinking — potential restructuring value")
    # Modifier: if high growth but strong FCF yield, reduce penalty
    if pts < 0:
        ev = stock.enterprise_value
        denom = ev if ev and ev > 0 else stock.market_cap
        if stock.free_cash_flow and denom and denom > 0:
            fcf_y = stock.free_cash_flow / denom
            if fcf_y > 0.05:
                pts = int(pts * 0.5)
                reasons.append("Penalty reduced — strong FCF yield")
    return pts, reasons


def _score_institutional_ownership(stock: ScoredStock) -> tuple[int, list[str]]:
    """Institutional ownership signal."""
    inst = stock.held_pct_institutions
    if inst is None:
        return 0, []
    pct = inst * 100 if inst < 1 else inst
    if pct < 15:
        return 4, [f"Low institutional ownership {pct:.0f}% — undiscovered"]
    if pct < 30:
        return 2, []
    if pct > 80:
        return -3, [f"Crowded institutional ownership {pct:.0f}%"]
    return 0, []


def _score_shareholder_yield(stock: ScoredStock) -> tuple[int, list[str]]:
    """Combined shareholder yield (dividend + buyback)."""
    sy = stock.shareholder_yield
    if sy is None or sy <= 0:
        return 0, []
    if sy > 0.08:
        return 6, [f"High shareholder yield {sy*100:.1f}%"]
    if sy > 0.05:
        return 4, [f"Solid shareholder yield {sy*100:.1f}%"]
    if sy > 0.03:
        return 2, []
    return 0, []


def _score_debt_maturity(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Debt maturity refinancing risk."""
    if sector_type in ("utility", "reit"):
        return 0, []
    mr = stock.debt_maturity_ratio
    if mr is None:
        return 0, []
    pts = 0
    reasons: list[str] = []
    if mr > 0.50:
        pts = -8; reasons.append(f"High near-term debt maturity ({mr*100:.0f}% of LTD due soon)")
    elif mr > 0.30:
        pts = -4; reasons.append(f"Elevated debt maturity ratio ({mr*100:.0f}%)")
    # Compound: high maturity + thin interest coverage
    ic = stock.interest_coverage
    if pts < 0 and ic is not None and ic < 2.0:
        pts -= 5; reasons.append("Maturity risk + thin coverage = refinancing risk")
    return pts, reasons


def _score_ncav(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Graham NCAV (Net-Net) deep value bonus."""
    if sector_type in ("financial", "reit"):
        return 0, []
    ncav_ps = stock.ncav_per_share
    price = stock.price
    if ncav_ps is None or price is None or price <= 0 or ncav_ps <= 0:
        return 0, []
    ratio = price / ncav_ps
    pts = 0
    reasons: list[str] = []
    if ratio < 1.0:
        pts = 15; reasons.append(f"True net-net: trading below NCAV ({ratio:.1f}x)")
    elif ratio < 1.5:
        pts = 8; reasons.append(f"Near net-net value ({ratio:.1f}x NCAV)")
    elif ratio < 2.0:
        pts = 3; reasons.append(f"Close to NCAV ({ratio:.1f}x)")
    # F-Score cross-check
    if pts > 0:
        fs = stock.piotroski_f_score
        if fs is not None and fs >= 7:
            pts += 5; reasons.append("NCAV + strong F-Score")
        elif fs is not None and fs <= 3:
            pts = int(pts * 0.5); reasons.append("NCAV discounted — weak fundamentals")
    return pts, reasons


def _is_biotech(stock: ScoredStock) -> bool:
    """Detect pre-revenue biotech within Healthcare sector."""
    if stock.sector != "Healthcare":
        return False
    ebitda = stock.ebitda
    rev = stock.gross_profit  # use as proxy for revenue scale
    dy = stock.dividend_yield
    gp = stock.gross_profit
    ta = stock.total_assets
    if ebitda is None or ebitda >= 0:
        return False
    # Revenue below $100M or no gross profit
    total_rev = None
    if stock.price_to_sales and stock.market_cap and stock.price_to_sales > 0:
        total_rev = stock.market_cap / stock.price_to_sales
    if total_rev and total_rev > 100_000_000:
        return False
    if dy and dy > 0:
        return False
    return True


def _score_biotech_cash_runway(stock: ScoredStock) -> tuple[int, list[str]]:
    """Cash runway signal for pre-revenue biotech."""
    cash = (stock.total_cash or 0)
    ocf = stock.operating_cashflow
    if ocf is None or ocf >= 0:
        return -5, ["Biotech: cannot compute cash runway"]
    quarterly_burn = abs(ocf) / 4
    if quarterly_burn <= 0:
        return -5, ["Biotech: cannot compute cash runway"]
    runway = cash / quarterly_burn
    if runway > 8:
        return 8, [f"Biotech: {runway:.0f} quarters cash runway"]
    if runway > 6:
        return 4, [f"Biotech: {runway:.0f} quarters runway"]
    if runway > 4:
        return 0, [f"Biotech: {runway:.0f} quarters runway"]
    if runway > 2:
        return -10, [f"Biotech: only {runway:.1f} quarters runway — dilution risk"]
    return -20, [f"Biotech: critical — {runway:.1f} quarters runway"]


# =====================================================================
# Universal scoring wrapper
# =====================================================================

def _score_universal(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """All scoring signals integrated into universal pipeline."""
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

    # Change 10: Net Debt/EBITDA leverage (replaces old D/E)
    p, r = _penalty_leverage(stock, sector_type)
    pts += p; reasons.extend(r)
    if p <= -10:
        red_flag_count += 1

    # Change 9: Altman Z-Score
    p, r = _score_altman_z(stock, sector_type)
    pts += p; reasons.extend(r)
    if p <= -10:
        red_flag_count += 1

    # Change 13: Interest coverage
    p, r = _score_interest_coverage(stock, sector_type)
    pts += p; reasons.extend(r)
    if p <= -10:
        red_flag_count += 1

    # Change 11: Dividend sustainability
    p, r = _score_dividend_sustainability(stock, sector_type)
    pts += p; reasons.extend(r)

    # Change A: Accruals quality (Value Score)
    p, r = _score_accruals_quality(stock, sector_type)
    pts += p; reasons.extend(r)
    if p <= -10:
        red_flag_count += 1

    # Beneish M-Score
    p, r = _score_beneish(stock, sector_type)
    pts += p; reasons.extend(r)
    if p <= -8:
        red_flag_count += 1

    # Asset growth penalty
    p, r = _score_asset_growth(stock, sector_type)
    pts += p; reasons.extend(r)

    # Shareholder yield bonus
    p, r = _score_shareholder_yield(stock)
    pts += p; reasons.extend(r)

    # Debt maturity risk
    p, r = _score_debt_maturity(stock, sector_type)
    pts += p; reasons.extend(r)
    if p <= -8:
        red_flag_count += 1

    # Graham NCAV net-net bonus
    p, r = _score_ncav(stock, sector_type)
    pts += p; reasons.extend(r)

    # Change C: Historical mean reversion (Value Score)
    p, r = _score_historical_mean_reversion(stock, sector_type)
    pts += p; reasons.extend(r)

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
        # P/FFO replaces EV/EBITDA for REITs
        p, r = _score_p_ffo(stock)
        pts += p; reasons.extend(r)
    elif sector_type == "energy":
        p, r = _score_energy_pcf(stock)
        pts += p; reasons.extend(r)
    elif sector_type == "comms":
        p, r = _score_china_adr(stock)
        pts += p; reasons.extend(r)

    # EV/Gross Profit for Default/Tech sector (10 pts)
    if sector_type == "default":
        evgp = stock.ev_gross_profit
        if evgp is not None and stock.gross_profit and stock.gross_profit > 0:
            if evgp < 3:
                pts += 10; reasons.append(f"Very low EV/Gross Profit ({evgp:.1f}x)")
            elif evgp < 6:
                pts += 7; reasons.append(f"Low EV/Gross Profit ({evgp:.1f}x)")
            elif evgp < 10:
                pts += 4
            elif evgp < 15:
                pts += 1

    # Biotech detection and cash runway (Healthcare)
    if sector_type == "healthcare" and _is_biotech(stock):
        # Skip normal valuation metrics — already applied via rubric but biotech overrides
        p, r = _score_biotech_cash_runway(stock)
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


# =====================================================================
# Quality score — "great business at a fair price"
#
# This is the opposite lens from value scoring. It asks:
# "Is this a fundamentally excellent business that's temporarily on sale?"
# Weights quality metrics (F-Score, ROE, margins, growth) heavily and
# cheapness metrics lightly. Designed to surface stocks like HD, ADP,
# CTAS, RACE that score poorly on value but are quality compounders.
# =====================================================================

def compute_quality_score(
    stock: ScoredStock, peer_avg: Optional[SectorAverages]
) -> ScoreBreakdown:
    """Compute a quality-at-fair-price score (0-100)."""
    pts = 0
    reasons: list[str] = []

    # ── Piotroski F-Score (25 pts max — heaviest weight) ──
    fs = stock.piotroski_f_score
    if fs is not None:
        if fs >= 8:
            pts += 25; reasons.append(f"Excellent fundamentals (F-Score {fs}/9)")
        elif fs >= 7:
            pts += 20; reasons.append(f"Strong fundamentals (F-Score {fs}/9)")
        elif fs >= 6:
            pts += 12; reasons.append(f"Good fundamentals (F-Score {fs}/9)")
        elif fs >= 4:
            pts += 4
        else:
            pts -= 10; reasons.append(f"Weak fundamentals (F-Score {fs}/9)")

    # ── Change 4: ROIC (15 pts max — leverage-neutral, skip financial/reit) ──
    roic = stock.roic
    st = detect_sector_type(stock.sector, stock.industry)
    if roic is not None and st not in ("financial", "reit"):
        if roic > 0.25:
            pts += 10; reasons.append(f"Exceptional ROIC {roic*100:.0f}%")
        elif roic > 0.15:
            pts += 7; reasons.append(f"Strong ROIC {roic*100:.0f}%")
        elif roic > 0.08:
            pts += 4
        elif roic > 0:
            pts += 2
        elif roic < 0:
            pts -= 6; reasons.append("Negative ROIC — destroying capital")

    # ── ROE vs sector (15 pts max — complements ROIC) ──
    roe = stock.return_on_equity
    mkt_roe = stock.market_avg_roe or (peer_avg.avg_roe if peer_avg else None)
    if roe is not None and roe > 0:
        if mkt_roe and mkt_roe > 0:
            ratio = roe / mkt_roe
            if ratio >= 2.0:
                pts += 15; reasons.append(f"ROE {roe*100:.0f}% — {ratio:.1f}x sector")
            elif ratio >= 1.3:
                pts += 10; reasons.append(f"ROE {roe*100:.0f}% — above sector")
            elif ratio >= 0.8:
                pts += 5
            else:
                pts += 2
        elif roe > 0.20:
            pts += 12; reasons.append(f"Strong ROE {roe*100:.0f}%")
        elif roe > 0.12:
            pts += 6
    elif roe is not None and roe < 0:
        pts -= 5; reasons.append("Negative ROE")

    # ── Earnings growth (15 pts max) ──
    eg = stock.earnings_growth
    if eg is not None:
        if eg > 0.25:
            pts += 15; reasons.append(f"Earnings growing {eg*100:.0f}%")
        elif eg > 0.10:
            pts += 10; reasons.append(f"Earnings growing {eg*100:.0f}%")
        elif eg > 0:
            pts += 5
        elif eg > -0.15:
            pts -= 3
        elif eg > -0.40:
            pts -= 8; reasons.append(f"Earnings declining {eg*100:.0f}%")
        else:
            pts -= 15; reasons.append(f"Earnings collapsing {eg*100:.0f}%")

    # ── Change 6: Enhanced revenue growth tiers (10 pts max) ──
    rg = stock.revenue_growth
    if rg is not None:
        if rg > 0.15:
            pts += 10; reasons.append(f"Strong revenue growth {rg*100:.0f}%")
        elif rg > 0.08:
            pts += 7; reasons.append(f"Solid revenue growth {rg*100:.0f}%")
        elif rg > 0.03:
            pts += 4; reasons.append("Positive revenue growth")
        elif rg > 0:
            pts += 2; reasons.append("Modest but positive growth")
        elif rg > -0.05:
            pts -= 3
        elif rg > -0.15:
            pts -= 8; reasons.append(f"Revenue declining {rg*100:.0f}%")
        else:
            pts -= 15; reasons.append(f"Revenue collapsing {rg*100:.0f}%")
    # Revenue acceleration bonus
    ra = stock.revenue_acceleration
    if ra is not None and ra > 0.05:
        pts += 4; reasons.append("Revenue accelerating")

    # ── Change 3: Gross margin trend (14 pts max) ──
    gm = stock.gross_margin_change
    if gm is not None:
        if gm > 0.02:
            pts += 14; reasons.append(f"Meaningful gross margin expansion +{gm*100:.0f}bps")
        elif gm > 0.005:
            pts += 8; reasons.append("Gross margin expanding")
        elif abs(gm) <= 0.005:
            pts += 4; reasons.append("Stable margins")
        elif gm < -0.04:
            pts -= 15; reasons.append(f"Significant margin deterioration {gm*100:.0f}bps")
        elif gm < -0.02:
            pts -= 10; reasons.append(f"Margin compression {gm*100:.0f}bps")
        else:
            pts -= 5; reasons.append(f"Margins contracting {gm*100:.0f}bps")

    # ── FCF yield on EV (8 pts max) ──
    fcf = stock.free_cash_flow
    denom = stock.enterprise_value if stock.enterprise_value and stock.enterprise_value > 0 else stock.market_cap
    if fcf and denom and denom > 0:
        fy = fcf / denom
        if fy > 0.08:
            pts += 8; reasons.append(f"Strong FCF yield {fy*100:.0f}%")
        elif fy > 0.03:
            pts += 4
        elif fy < -0.05:
            pts -= 5; reasons.append("Burning cash")

    # ── Buyback yield (8 pts max when near 52W low) ──
    bb = stock.buyback_yield
    if bb is not None:
        if bb > 0.02:
            # Increase to +8 when at bottom of 52W range (all stocks in scan qualify)
            bb_pts = 8 if (stock.fifty_two_week_high and stock.price and stock.fifty_two_week_high > 0 and (stock.price - stock.fifty_two_week_low) / (stock.fifty_two_week_high - stock.fifty_two_week_low) < 0.20) else 5
            pts += bb_pts; reasons.append(f"Active buybacks at 52W low (+{bb_pts})")
        elif bb < -0.03:
            pts -= 3; reasons.append("Share dilution")

    # ── Insider confidence (10 pts max) ──
    buys = stock.insider_buy_count
    sells = stock.insider_sell_count
    total = buys + sells
    if total > 0:
        sentiment = buys / total
        if sentiment > 0.7 and buys >= 2:
            pts += 10; reasons.append(f"Insiders buying ({buys}B/{sells}S)")
        elif sentiment > 0.5:
            pts += 4
        elif sells >= 10:
            pts -= 8; reasons.append(f"Heavy insider selling ({sells}S)")
        elif sells >= 5:
            pts -= 4; reasons.append(f"Insider selling ({sells}S)")

    # ── Low short interest (5 pts max) ──
    si = stock.short_percent_of_float
    if si is not None:
        pct = si * 100 if si < 1 else si
        if pct < 3:
            pts += 5; reasons.append("Low short interest")
        elif pct > 25:
            pts -= 5; reasons.append(f"High short interest {pct:.0f}%")

    # ── Price drop as opportunity (10 pts max) ──
    # Sharp drop from 52W high = bigger opportunity for quality business
    if stock.fifty_two_week_high and stock.price and stock.fifty_two_week_high > 0:
        drop = 1 - (stock.price / stock.fifty_two_week_high)
        if drop > 0.40:
            pts += 10; reasons.append(f"Down {drop*100:.0f}% from 52W high")
        elif drop > 0.25:
            pts += 6; reasons.append(f"Down {drop*100:.0f}% from 52W high")
        elif drop > 0.15:
            pts += 3

    # ── Change 5: Relative momentum in quality score ──
    rm = stock.relative_momentum
    if rm is not None:
        if rm > 15:
            pts += 8; reasons.append(f"Strong positive relative momentum (+{rm:.0f}pp)")
        elif rm > 5:
            pts += 5; reasons.append("Outperforming sector peers")
        elif rm > 0:
            pts += 3; reasons.append("Slightly ahead of sector")
        elif rm < -20:
            pts -= 5; reasons.append("Underperforming sector peers badly")

    # ── Change 7: Analyst upside in quality score ──
    p, r = _score_upside(stock)
    pts += p; reasons.extend(r)

    # ── Mild valuation bonus ──
    fpe = stock.forward_pe
    mkt_fpe = stock.market_avg_fpe or (peer_avg.avg_forward_pe if peer_avg else None)
    if fpe and fpe > 0 and mkt_fpe and mkt_fpe > 0:
        ratio = fpe / mkt_fpe
        if ratio < 0.50:
            pts += 5; reasons.append("Also cheap on P/E")
        elif ratio < 0.75:
            pts += 2

    # Change A: Accruals quality (Quality Score)
    p, r = _score_accruals_quality_q(stock, st)
    pts += p; reasons.extend(r)

    # Gross profitability (Novy-Marx)
    p, r = _score_gp_to_assets(stock, st)
    pts += p; reasons.extend(r)

    # ROIC vs WACC spread
    p, r = _score_roic_vs_wacc(stock, st)
    pts += p; reasons.extend(r)

    # Institutional ownership
    p, r = _score_institutional_ownership(stock)
    pts += p; reasons.extend(r)

    # Change B: EPS revision momentum (Quality Score)
    p, r = _score_eps_revision(stock)
    pts += p; reasons.extend(r)

    score = max(0, min(100, pts))
    if score >= 65:
        tier = "Quality Buy"
    elif score >= 45:
        tier = "Quality Watch"
    else:
        tier = "Not Quality"

    return ScoreBreakdown(total=score, tier=tier, reasons=reasons, sector_type=st)
