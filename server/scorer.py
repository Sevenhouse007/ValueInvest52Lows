"""Sector-aware value scoring engine.

Each sector uses a different rubric reflecting how Wall Street actually
values companies in that sector.  The master function ``compute_score``
detects the sector type and dispatches to the appropriate scorer.
"""

from __future__ import annotations

from typing import Optional

from server.models import ScoreBreakdown, ScoredStock, SectorAverages

# ── China ADR detection via country field ────────────────────────────
_CHINA_COUNTRIES = {"China", "Hong Kong"}


# =====================================================================
# Sector detection
# =====================================================================

def detect_sector_type(sector: str, industry: str) -> str:
    """Map Yahoo Finance sector/industry to an internal scoring type."""
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
# Shared helpers
# =====================================================================

def _roe_pct(roe: Optional[float]) -> Optional[float]:
    """Normalise ROE to a percentage value (0-100 scale)."""
    if roe is None:
        return None
    return roe * 100 if abs(roe) < 1 else roe


def _upside(stock: ScoredStock) -> Optional[float]:
    """Compute upside ratio and set stock.upside_percent as side-effect."""
    if stock.target_mean_price and stock.price and stock.price > 0:
        u = (stock.target_mean_price - stock.price) / stock.price
        stock.upside_percent = round(u * 100, 1)
        return u
    return None


def _score_analyst_rec(rec: Optional[float], max_pts: int = 12) -> tuple[int, list[str]]:
    """Standard analyst recommendation scoring."""
    if rec is None or rec <= 0:
        return 0, []
    if max_pts >= 12:
        if rec < 1.8:
            return 12, ["Analyst: Strong Buy"]
        if rec < 2.3:
            return 7, ["Analyst: Buy"]
        if rec > 3.5:
            return -5, []
    elif max_pts >= 10:
        if rec < 1.8:
            return 10, ["Analyst: Strong Buy"]
        if rec < 2.3:
            return 5, ["Analyst: Buy"]
        if rec > 3.5:
            return -5, []
    elif max_pts >= 8:
        if rec < 1.8:
            return 8, ["Analyst: Strong Buy"]
        if rec < 2.3:
            return 4, ["Analyst: Buy"]
    return 0, []


def _score_upside(stock: ScoredStock, max_pts: int = 6,
                  high_thresh: float = 0.40, low_thresh: float = 0.20) -> tuple[int, list[str]]:
    """Analyst target upside — capped at 6 pts (targets lag at 52W lows)."""
    u = _upside(stock)
    if u is None:
        return 0, []
    pct_label = f"{stock.upside_percent:.0f}% upside"
    if u > high_thresh:
        return min(max_pts, 6), [pct_label]
    if u > low_thresh:
        return min(max(max_pts - 2, 2), 4), [pct_label]
    return 0, []


def _score_growth(stock: ScoredStock) -> tuple[int, list[str]]:
    """Score earnings and revenue growth direction.

    Penalizes deteriorating fundamentals — important for 52W-low stocks
    to distinguish 'cheap & stable' from 'cheap & collapsing'.
    """
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
    """Bonus for stocks trading very close to their 52-week low.

    Range position = (price - low) / (high - low).
    Closer to 0 = nearer the bottom.
    """
    low = stock.fifty_two_week_low
    high = stock.fifty_two_week_high
    price = stock.price
    if not low or not high or high <= low or not price:
        return 0, []

    position = (price - low) / (high - low)

    if position < 0.05:
        return 8, [f"At 52W low (bottom {position*100:.0f}% of range)"]
    if position < 0.10:
        return 5, [f"Near 52W low ({position*100:.0f}% from bottom)"]
    if position < 0.20:
        return 3, [f"Close to 52W low ({position*100:.0f}% from bottom)"]
    return 0, []


def _penalty_missing_forward_pe(fpe: Optional[float], sector_type: str) -> tuple[int, list[str]]:
    """Penalize negative or missing forward P/E in sectors where it matters.

    REITs and financials are excluded (P/E is misleading for them).
    """
    if sector_type in ("reit", "financial"):
        return 0, []  # P/E not meaningful for these sectors
    if fpe is not None and fpe < 0:
        return -8, ["Negative forward earnings"]
    if fpe is None:
        return -4, ["No forward earnings estimate"]
    return 0, []


def _score_short_interest(stock: ScoredStock) -> tuple[int, list[str]]:
    """Score short interest — heavy shorting at a 52W low is a strong warning.

    However, high short + insider buying = potential squeeze, so reduce
    the penalty when insiders are buying against the shorts.
    """
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
    """Score insider transactions — insiders selling at a 52W low is a red flag.

    Scaled by severity. Insider buying offsets selling concern when both occur
    (e.g., 4 buys + 3 sells = mixed, not alarming).
    """
    buys = stock.insider_buy_count
    sells = stock.insider_sell_count
    total = buys + sells
    if total == 0:
        return 0, []
    sentiment = buys / total

    # Mixed activity: both significant buying and selling — treat as neutral-ish
    if buys >= 3 and sells >= 3:
        if sentiment > 0.5:
            return 2, [f"Mixed insider activity, net buying ({buys}B/{sells}S)"]
        return -2, [f"Mixed insider activity, net selling ({buys}B/{sells}S)"]

    # Strong buying — management has conviction
    if sentiment > 0.7 and buys >= 3:
        return 10, [f"Strong insider buying ({buys} buys vs {sells} sells)"]
    if sentiment > 0.7 and buys >= 2:
        return 6, [f"Insider buying ({buys} buys vs {sells} sells)"]
    if sentiment > 0.5:
        return 3, [f"Net insider buying ({buys}B/{sells}S)"]

    # Selling — scaled by volume, no buying to offset
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
    """Score Piotroski F-Score — weak fundamentals at a 52W low = value trap."""
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


def _penalty_sector_relative(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Penalize metrics that deviate extremely from the sector/market average.

    Instead of hardcoded absolute thresholds, compares each metric against
    the market average for that sector. A stock with D/E at 5x its sector
    average is penalized much harder than one at 1.5x, regardless of the
    absolute number.

    Checks: Debt/Equity, EV/EBITDA (overvaluation), negative FCF burn rate.
    """
    pts = 0
    reasons: list[str] = []

    # --- Debt/Equity vs market average ---
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
        # Fallback if no market avg available
        pts -= 10; reasons.append(f"Very high leverage D/E {de:.0f}")

    # --- EV/EBITDA much higher than sector (overvalued even at 52W low) ---
    ev = stock.ev_to_ebitda
    mkt_ev = stock.market_avg_ev_ebitda
    if ev is not None and ev > 0 and mkt_ev and mkt_ev > 0:
        ratio = ev / mkt_ev
        if ratio > 2.5:
            pts -= 10; reasons.append(f"EV/EBITDA {ratio:.1f}x sector avg — still expensive")
        elif ratio > 1.8:
            pts -= 5; reasons.append(f"EV/EBITDA above sector avg")

    # --- Negative FCF burn rate (absolute — cash burn has no "sector avg") ---
    fcf = stock.free_cash_flow
    mc = stock.market_cap
    if fcf is not None and fcf < 0 and mc and mc > 0:
        burn_rate = abs(fcf) / mc
        if burn_rate > 0.50:
            pts -= 15; reasons.append(f"Extreme cash burn ({burn_rate*100:.0f}% of mkt cap)")
        elif burn_rate > 0.20:
            pts -= 8; reasons.append(f"Heavy cash burn ({burn_rate*100:.0f}% of mkt cap)")
        elif burn_rate > 0.05:
            pts -= 3; reasons.append(f"Negative FCF ({burn_rate*100:.0f}% of mkt cap)")

    # --- ROE much worse than sector ---
    roe = stock.return_on_equity
    mkt_roe = stock.market_avg_roe
    if roe is not None and mkt_roe and mkt_roe > 0:
        if roe < 0 and mkt_roe > 0.05:
            pts -= 5; reasons.append(f"Negative ROE vs sector avg {mkt_roe*100:.0f}%")

    return pts, reasons


def _score_universal(stock: ScoredStock, sector_type: str) -> tuple[int, list[str]]:
    """Universal add-on signals applied to all sector scorers.

    When multiple red flags stack (declining earnings + insider selling +
    weak Piotroski), applies an extra compound penalty — the combination
    is worse than the sum of parts.
    """
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

    # Missing forward P/E penalty
    p, r = _penalty_missing_forward_pe(stock.forward_pe, sector_type)
    pts += p; reasons.extend(r)

    # Sector-relative deviation penalties (debt, EV/EBITDA, cash burn, ROE)
    p, r = _penalty_sector_relative(stock, sector_type)
    pts += p; reasons.extend(r)
    if p <= -10:
        red_flag_count += 1

    # Compound penalty for stacking red flags
    if red_flag_count >= 3:
        pts -= 15; reasons.append("Multiple severe red flags — high value trap risk")
    elif red_flag_count >= 2:
        pts -= 8; reasons.append("Compounding red flags")

    return pts, reasons


def _safe_avg(avg: Optional[SectorAverages], attr: str) -> Optional[float]:
    if avg is None:
        return None
    return getattr(avg, attr, None)


def _fcf_yield(stock: ScoredStock) -> Optional[float]:
    """FCF yield = FCF / market_cap. Returns as ratio (e.g. 0.10 = 10%)."""
    fcf = stock.free_cash_flow
    mc = stock.market_cap
    if fcf is None or not mc or mc <= 0:
        return None
    return fcf / mc


def _score_fcf_yield(stock: ScoredStock, max_pts: int = 18) -> tuple[int, list[str]]:
    """Score FCF using yield (FCF/market_cap) instead of absolute value."""
    fy = _fcf_yield(stock)
    if fy is None:
        return 0, []
    if fy < -0.02:
        return -8, ["Negative FCF yield"]
    if fy < 0:
        return -3, []
    pct = fy * 100
    if max_pts >= 18:
        if fy > 0.12:
            return 18, [f"Strong FCF yield {pct:.0f}%"]
        if fy > 0.07:
            return 14, [f"Solid FCF yield {pct:.0f}%"]
        if fy > 0.03:
            return 8, ["Positive FCF"]
        if fy > 0:
            return 4, ["Positive FCF"]
    elif max_pts >= 15:
        if fy > 0.12:
            return 15, [f"Strong FCF yield {pct:.0f}%"]
        if fy > 0.07:
            return 12, [f"Solid FCF yield {pct:.0f}%"]
        if fy > 0.03:
            return 7, ["Positive FCF"]
        if fy > 0:
            return 3, ["Positive FCF"]
    elif max_pts >= 12:
        if fy > 0.10:
            return 12, [f"Strong FCF yield {pct:.0f}%"]
        if fy > 0.05:
            return 8, ["Positive FCF"]
        if fy > 0:
            return 4, ["Positive FCF"]
    else:
        if fy > 0.10:
            return max_pts, [f"Strong FCF yield {pct:.0f}%"]
        if fy > 0.03:
            return max(max_pts - 4, 2), ["Positive FCF"]
        if fy > 0:
            return 2, ["Positive FCF"]
    return 0, []


def _penalty_negative_ev_ebitda(ev: Optional[float]) -> tuple[int, list[str]]:
    """Penalize negative EV/EBITDA (negative EBITDA = operating losses)."""
    if ev is not None and ev < 0:
        return -12, ["Negative EBITDA — operating losses"]
    return 0, []


def _is_china_adr(stock: ScoredStock) -> bool:
    """Detect China/HK ADR using the country field from assetProfile."""
    return (stock.country or "") in _CHINA_COUNTRIES


# =====================================================================
# 1. Financial (banks, insurance, asset management)
# =====================================================================

def _score_financial(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    # P/B (30 pts max)
    pb = stock.price_to_book
    if pb is not None and pb > 0:
        if pb < 0.8:
            pts += 30; reasons.append("Trading below book value")
        elif pb < 1.0:
            pts += 24; reasons.append("Near book value")
        elif pb < 1.2:
            pts += 16
        elif pb < 1.5:
            pts += 8
        avg_pb = _safe_avg(avg, "avg_price_to_book")
        if avg_pb and avg_pb > 0 and pb < avg_pb * 0.80:
            pts += 10; reasons.append("Cheap vs sector P/B")

    # ROE (25 pts max)
    roe = _roe_pct(stock.return_on_equity)
    if roe is not None:
        if roe > 18:
            pts += 25; reasons.append(f"Strong ROE {roe:.0f}%")
        elif roe > 12:
            pts += 18; reasons.append("Solid ROE")
        elif roe > 8:
            pts += 10
        elif roe > 0:
            pts += 4
        else:
            pts -= 15; reasons.append("Negative ROE")

    # ROA (15 pts max)
    roa = stock.return_on_assets
    if roa is not None:
        if roa > 0.015:
            pts += 15; reasons.append("ROA above 1.5%")
        elif roa > 0.010:
            pts += 10; reasons.append("ROA above 1%")
        elif roa > 0.005:
            pts += 5
        elif roa < 0:
            pts -= 8

    # Forward P/E (15 pts max)
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 8:
            pts += 15
        elif fpe < 12:
            pts += 10
        elif fpe < 16:
            pts += 6
        avg_fpe = _safe_avg(avg, "avg_forward_pe")
        if avg_fpe and avg_fpe > 0 and fpe < avg_fpe * 0.80:
            pts += 8; reasons.append("Cheap vs sector P/E")

    # Analyst (12 pts)
    p, r = _score_analyst_rec(stock.recommendation_mean, 12)
    pts += p; reasons.extend(r)

    # Upside (10 pts)
    p, r = _score_upside(stock, 10)
    pts += p; reasons.extend(r)

    # EV/EBITDA: ALWAYS 0 for financials
    # FCF: ALWAYS 0 for financials

    p, r = _score_universal(stock, "financial")
    pts += p; reasons.extend(r)

    return pts, reasons


# =====================================================================
# 2. REIT
# =====================================================================

def _score_reit(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    # Dividend yield (30 pts max)
    dy = stock.dividend_yield
    if dy is not None:
        if dy > 0.07:
            pts += 30; reasons.append(f"High dividend yield {dy*100:.1f}%")
        elif dy > 0.05:
            pts += 22; reasons.append("Strong dividend yield")
        elif dy > 0.04:
            pts += 14; reasons.append("Solid dividend yield")
        elif dy > 0.03:
            pts += 7
        elif dy <= 0:
            pts -= 10; reasons.append("No dividend — unusual for REIT")

    # P/B as NAV proxy (25 pts max)
    pb = stock.price_to_book
    if pb is not None and pb > 0:
        if pb < 0.9:
            pts += 25; reasons.append("Trading below NAV (P/B < 1)")
        elif pb < 1.1:
            pts += 18; reasons.append("Near NAV")
        elif pb < 1.4:
            pts += 10
        elif pb < 1.8:
            pts += 5
        avg_pb = _safe_avg(avg, "avg_price_to_book")
        if avg_pb and avg_pb > 0 and pb < avg_pb * 0.85:
            pts += 8; reasons.append("Cheap vs sector P/B")

    # Forward P/E (LOW weight, 10 pts — use cautiously)
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 15:
            pts += 10
        elif fpe < 25:
            pts += 5
        # Never penalize REITs for high P/E

    # EV/EBITDA (15 pts)
    ev = stock.ev_to_ebitda
    if ev is not None and ev > 0:
        if ev < 10:
            pts += 15; reasons.append("Low EV/EBITDA")
        elif ev < 14:
            pts += 10
        elif ev < 18:
            pts += 5
        avg_ev = _safe_avg(avg, "avg_ev_to_ebitda")
        if avg_ev and avg_ev > 0 and ev < avg_ev * 0.85:
            pts += 8; reasons.append("Cheap vs sector EV/EBITDA")
    p, r = _penalty_negative_ev_ebitda(ev)
    pts += p; reasons.extend(r)

    # ROE (10 pts)
    roe = _roe_pct(stock.return_on_equity)
    if roe is not None:
        if roe > 10:
            pts += 10; reasons.append("Positive ROE")
        elif roe > 5:
            pts += 5
        elif roe < 0:
            pts -= 5

    # Analyst (10 pts)
    p, r = _score_analyst_rec(stock.recommendation_mean, 10)
    pts += p; reasons.extend(r)

    # Upside (10 pts)
    p, r = _score_upside(stock, 10, high_thresh=0.30, low_thresh=0.15)
    pts += p; reasons.extend(r)

    # P/E: ALWAYS 0 weight (handled above with low weight only)
    # FCF: ALWAYS 0 weight for REITs

    p, r = _score_universal(stock, "reit")
    pts += p; reasons.extend(r)

    return pts, reasons


# =====================================================================
# 3. Energy
# =====================================================================

def _score_energy(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    # EV/EBITDA (28 pts max)
    ev = stock.ev_to_ebitda
    if ev is not None and ev > 0:
        if ev < 4:
            pts += 28; reasons.append("Very cheap EV/EBITDA")
        elif ev < 6:
            pts += 22; reasons.append("Low EV/EBITDA")
        elif ev < 8:
            pts += 14
        elif ev < 10:
            pts += 7
        avg_ev = _safe_avg(avg, "avg_ev_to_ebitda")
        if avg_ev and avg_ev > 0 and ev < avg_ev * 0.80:
            pts += 10; reasons.append("Cheap vs energy peers")
    p, r = _penalty_negative_ev_ebitda(ev)
    pts += p; reasons.extend(r)

    # Price/Cash Flow (25 pts max)
    ocf = stock.operating_cashflow
    mc = stock.market_cap
    if ocf and ocf > 0 and mc and mc > 0:
        pcf = mc / ocf
        if pcf < 5:
            pts += 25; reasons.append("Very low P/CF")
        elif pcf < 8:
            pts += 18; reasons.append("Cheap on cash flow")
        elif pcf < 12:
            pts += 10
        elif pcf < 16:
            pts += 5

    # FCF yield (15 pts)
    p, r = _score_fcf_yield(stock, 15)
    pts += p; reasons.extend(r)

    # Forward P/E (12 pts)
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 8:
            pts += 12
        elif fpe < 12:
            pts += 8
        elif fpe < 16:
            pts += 4

    # Dividend yield (10 pts)
    dy = stock.dividend_yield
    if dy is not None and dy > 0:
        if dy > 0.05:
            pts += 10; reasons.append("High dividend yield")
        elif dy > 0.03:
            pts += 6
        elif dy > 0.01:
            pts += 3

    # ROE (8 pts)
    roe = _roe_pct(stock.return_on_equity)
    if roe is not None:
        if roe > 20:
            pts += 8; reasons.append("Strong ROE")
        elif roe > 10:
            pts += 4
        elif roe < 0:
            pts -= 5

    # Analyst (8 pts)
    p, r = _score_analyst_rec(stock.recommendation_mean, 8)
    pts += p; reasons.extend(r)

    # Upside (8 pts)
    p, r = _score_upside(stock, 8, high_thresh=0.35, low_thresh=0.20)
    pts += p; reasons.extend(r)

    # P/B: WEIGHT 0 for energy

    p, r = _score_universal(stock, "energy")
    pts += p; reasons.extend(r)

    return pts, reasons


# =====================================================================
# 4. Healthcare
# =====================================================================

def _score_healthcare(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    # EV/EBITDA (25 pts)
    ev = stock.ev_to_ebitda
    if ev is not None and ev > 0:
        if ev < 8:
            pts += 25; reasons.append("Very cheap EV/EBITDA")
        elif ev < 12:
            pts += 18
        elif ev < 16:
            pts += 10
        elif ev < 20:
            pts += 5
        avg_ev = _safe_avg(avg, "avg_ev_to_ebitda")
        if avg_ev and avg_ev > 0 and ev < avg_ev * 0.80:
            pts += 10; reasons.append("Cheap vs healthcare peers")
    p, r = _penalty_negative_ev_ebitda(ev)
    pts += p; reasons.extend(r)

    # Forward P/E (22 pts)
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 12:
            pts += 22; reasons.append("Low forward P/E")
        elif fpe < 18:
            pts += 15
        elif fpe < 25:
            pts += 8
        elif fpe < 30:
            pts += 4
        avg_fpe = _safe_avg(avg, "avg_forward_pe")
        if avg_fpe and avg_fpe > 0 and fpe < avg_fpe * 0.80:
            pts += 8; reasons.append("Cheap vs sector P/E")

    # FCF yield (18 pts)
    p, r = _score_fcf_yield(stock, 18)
    pts += p; reasons.extend(r)

    # ROE (12 pts)
    roe = _roe_pct(stock.return_on_equity)
    if roe is not None:
        if roe > 20:
            pts += 12; reasons.append("Strong ROE")
        elif roe > 12:
            pts += 8
        elif roe > 0:
            pts += 4
        else:
            pts -= 8

    # P/B (8 pts)
    pb = stock.price_to_book
    if pb is not None and pb > 0:
        if pb < 1.5:
            pts += 8
        elif pb < 2.5:
            pts += 5
        elif pb < 4.0:
            pts += 2

    # Analyst (12 pts)
    p, r = _score_analyst_rec(stock.recommendation_mean, 12)
    pts += p; reasons.extend(r)

    # Upside (10 pts)
    p, r = _score_upside(stock, 10, high_thresh=0.35, low_thresh=0.20)
    pts += p; reasons.extend(r)

    # Debt penalty (unique to healthcare)
    de = stock.debt_to_equity
    if de is not None:
        if de > 200:
            pts -= 10; reasons.append("High leverage")
        elif de > 100:
            pts -= 5

    p, r = _score_universal(stock, "healthcare")
    pts += p; reasons.extend(r)

    return pts, reasons


# =====================================================================
# 5. Consumer Defensive (Staples)
# =====================================================================

def _score_staples(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    # EV/EBITDA (28 pts)
    ev = stock.ev_to_ebitda
    if ev is not None and ev > 0:
        if ev < 8:
            pts += 28; reasons.append("Very cheap EV/EBITDA")
        elif ev < 10:
            pts += 20; reasons.append("Low EV/EBITDA")
        elif ev < 13:
            pts += 12
        elif ev < 16:
            pts += 5
        avg_ev = _safe_avg(avg, "avg_ev_to_ebitda")
        if avg_ev and avg_ev > 0 and ev < avg_ev * 0.80:
            pts += 10; reasons.append("Cheap vs consumer staples peers")
    p, r = _penalty_negative_ev_ebitda(ev)
    pts += p; reasons.extend(r)

    # Forward P/E (22 pts)
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 10:
            pts += 22; reasons.append("Very low forward P/E")
        elif fpe < 14:
            pts += 15
        elif fpe < 18:
            pts += 8
        avg_fpe = _safe_avg(avg, "avg_forward_pe")
        if avg_fpe and avg_fpe > 0 and fpe < avg_fpe * 0.80:
            pts += 8; reasons.append("Cheap vs sector P/E")

    # FCF yield (18 pts)
    p, r = _score_fcf_yield(stock, 18)
    pts += p; reasons.extend(r)

    # Dividend yield (12 pts)
    dy = stock.dividend_yield
    if dy is not None and dy > 0:
        if dy > 0.05:
            pts += 12; reasons.append("High dividend yield")
        elif dy > 0.03:
            pts += 8
        elif dy > 0.02:
            pts += 4

    # ROE (10 pts)
    roe = _roe_pct(stock.return_on_equity)
    if roe is not None:
        if roe > 20:
            pts += 10
        elif roe > 12:
            pts += 6
        elif roe > 0:
            pts += 3
        else:
            pts -= 8

    # Analyst (10 pts)
    p, r = _score_analyst_rec(stock.recommendation_mean, 10)
    pts += p; reasons.extend(r)

    # Upside (10 pts)
    p, r = _score_upside(stock, 10)
    pts += p; reasons.extend(r)

    # P/B: WEIGHT 0 for staples

    p, r = _score_universal(stock, "staples")
    pts += p; reasons.extend(r)

    return pts, reasons


# =====================================================================
# 6. Consumer Cyclical
# =====================================================================

def _score_cyclical(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    # Forward P/E normalized (28 pts)
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 7:
            pts += 28; reasons.append("Very cheap normalized P/E")
        elif fpe < 10:
            pts += 22; reasons.append("Low P/E vs cycle")
        elif fpe < 14:
            pts += 12
        elif fpe < 18:
            pts += 5
        avg_fpe = _safe_avg(avg, "avg_forward_pe")
        if avg_fpe and avg_fpe > 0 and fpe < avg_fpe * 0.75:
            pts += 12; reasons.append("Deeply cheap vs sector")

    # P/B (20 pts)
    pb = stock.price_to_book
    if pb is not None and pb > 0:
        if pb < 1.0:
            pts += 20; reasons.append("Below book value")
        elif pb < 1.3:
            pts += 15; reasons.append("Near book value")
        elif pb < 2.0:
            pts += 8
        elif pb < 3.0:
            pts += 3
        avg_pb = _safe_avg(avg, "avg_price_to_book")
        if avg_pb and avg_pb > 0 and pb < avg_pb * 0.70:
            pts += 8; reasons.append("Cheap vs peers on P/B")

    # EV/EBITDA (18 pts)
    ev = stock.ev_to_ebitda
    if ev is not None and ev > 0:
        if ev < 5:
            pts += 18; reasons.append("Very low EV/EBITDA")
        elif ev < 8:
            pts += 13; reasons.append("Low EV/EBITDA")
        elif ev < 11:
            pts += 7
        avg_ev = _safe_avg(avg, "avg_ev_to_ebitda")
        if avg_ev and avg_ev > 0 and ev < avg_ev * 0.80:
            pts += 8; reasons.append("Cheap vs sector EV/EBITDA")
    p, r = _penalty_negative_ev_ebitda(ev)
    pts += p; reasons.extend(r)

    # FCF yield (12 pts)
    p, r = _score_fcf_yield(stock, 12)
    pts += p; reasons.extend(r)

    # ROE (10 pts)
    roe = _roe_pct(stock.return_on_equity)
    if roe is not None:
        if roe > 20:
            pts += 10; reasons.append("Strong ROE")
        elif roe > 12:
            pts += 6
        elif roe > 0:
            pts += 3
        else:
            pts -= 8

    # Analyst (10 pts)
    p, r = _score_analyst_rec(stock.recommendation_mean, 10)
    pts += p; reasons.extend(r)

    # Upside
    p, r = _score_upside(stock)
    pts += p; reasons.extend(r)

    p, r = _score_universal(stock, "cyclical")
    pts += p; reasons.extend(r)

    return pts, reasons


# =====================================================================
# 7. Industrials
# =====================================================================

def _score_industrial(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    # EV/EBITDA (25 pts)
    ev = stock.ev_to_ebitda
    if ev is not None and ev > 0:
        if ev < 6:
            pts += 25; reasons.append("Very cheap EV/EBITDA")
        elif ev < 9:
            pts += 18
        elif ev < 13:
            pts += 10
        elif ev < 17:
            pts += 4
        avg_ev = _safe_avg(avg, "avg_ev_to_ebitda")
        if avg_ev and avg_ev > 0 and ev < avg_ev * 0.80:
            pts += 10; reasons.append("Cheap vs industrial peers")
    p, r = _penalty_negative_ev_ebitda(ev)
    pts += p; reasons.extend(r)

    # Forward P/E (22 pts)
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 8:
            pts += 22
        elif fpe < 12:
            pts += 15
        elif fpe < 17:
            pts += 8
        elif fpe < 22:
            pts += 3
        avg_fpe = _safe_avg(avg, "avg_forward_pe")
        if avg_fpe and avg_fpe > 0 and fpe < avg_fpe * 0.80:
            pts += 8; reasons.append("Cheap vs sector P/E")

    # FCF yield (18 pts — critical for capex-intensive industrials)
    p, r = _score_fcf_yield(stock, 18)
    pts += p; reasons.extend(r)

    # ROE (12 pts)
    roe = _roe_pct(stock.return_on_equity)
    if roe is not None:
        if roe > 20:
            pts += 12; reasons.append("Strong ROE")
        elif roe > 12:
            pts += 8
        elif roe > 0:
            pts += 4
        else:
            pts -= 8

    # P/B (6 pts)
    pb = stock.price_to_book
    if pb is not None and pb > 0:
        if pb < 2.0:
            pts += 6
        elif pb < 3.0:
            pts += 3

    # Analyst (10 pts)
    p, r = _score_analyst_rec(stock.recommendation_mean, 10)
    pts += p; reasons.extend(r)

    # Upside (10 pts)
    p, r = _score_upside(stock, 10)
    pts += p; reasons.extend(r)

    # Debt penalty
    de = stock.debt_to_equity
    if de is not None:
        if de > 300:
            pts -= 10
        elif de > 150:
            pts -= 5

    p, r = _score_universal(stock, "industrial")
    pts += p; reasons.extend(r)

    return pts, reasons


# =====================================================================
# 8. Communication Services
# =====================================================================

def _score_comms(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    # EV/EBITDA (30 pts)
    ev = stock.ev_to_ebitda
    if ev is not None and ev > 0:
        if ev < 4:
            pts += 30; reasons.append("Extremely cheap EV/EBITDA")
        elif ev < 7:
            pts += 22; reasons.append("Low EV/EBITDA")
        elif ev < 10:
            pts += 13
        elif ev < 14:
            pts += 5
        avg_ev = _safe_avg(avg, "avg_ev_to_ebitda")
        if avg_ev and avg_ev > 0 and ev < avg_ev * 0.75:
            pts += 12; reasons.append("Deeply cheap vs peers")
    p, r = _penalty_negative_ev_ebitda(ev)
    pts += p; reasons.extend(r)

    # FCF yield (20 pts)
    p, r = _score_fcf_yield(stock, 18)
    pts += p; reasons.extend(r)

    # Forward P/E (18 pts)
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 8:
            pts += 18
        elif fpe < 12:
            pts += 12
        elif fpe < 17:
            pts += 6
        avg_fpe = _safe_avg(avg, "avg_forward_pe")
        if avg_fpe and avg_fpe > 0 and fpe < avg_fpe * 0.80:
            pts += 8; reasons.append("Cheap vs sector P/E")

    # ROE (12 pts)
    roe = _roe_pct(stock.return_on_equity)
    if roe is not None:
        if roe > 20:
            pts += 12
        elif roe > 12:
            pts += 8
        elif roe > 0:
            pts += 3
        else:
            pts -= 8

    # Analyst (10 pts)
    p, r = _score_analyst_rec(stock.recommendation_mean, 10)
    pts += p; reasons.extend(r)

    # Upside (10 pts)
    p, r = _score_upside(stock, 10)
    pts += p; reasons.extend(r)

    # Dividend yield (5 pts, only if > 3%)
    dy = stock.dividend_yield
    if dy is not None and dy > 0.03:
        pts += 5

    # P/B: WEIGHT 0 for comms

    # China ADR penalty (detected via country field)
    if _is_china_adr(stock):
        pts -= 20; reasons.append("China ADR — delisting/regulatory risk")

    p, r = _score_universal(stock, "comms")
    pts += p; reasons.extend(r)

    return pts, reasons


# =====================================================================
# 9. Basic Materials
# =====================================================================

def _score_materials(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    # EV/EBITDA normalized (28 pts)
    ev = stock.ev_to_ebitda
    if ev is not None and ev > 0:
        if ev < 5:
            pts += 28; reasons.append("Very cheap EV/EBITDA")
        elif ev < 7:
            pts += 20
        elif ev < 10:
            pts += 12
        elif ev < 13:
            pts += 5
        avg_ev = _safe_avg(avg, "avg_ev_to_ebitda")
        if avg_ev and avg_ev > 0 and ev < avg_ev * 0.80:
            pts += 10; reasons.append("Cheap vs materials peers")
    p, r = _penalty_negative_ev_ebitda(ev)
    pts += p; reasons.extend(r)

    # FCF yield (22 pts)
    p, r = _score_fcf_yield(stock, 18)
    pts += p; reasons.extend(r)

    # Forward P/E (14 pts)
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 8:
            pts += 14
        elif fpe < 12:
            pts += 10
        elif fpe < 16:
            pts += 5
        avg_fpe = _safe_avg(avg, "avg_forward_pe")
        if avg_fpe and avg_fpe > 0 and fpe < avg_fpe * 0.80:
            pts += 6; reasons.append("Cheap vs sector P/E")

    # P/B (12 pts)
    pb = stock.price_to_book
    if pb is not None and pb > 0:
        if pb < 1.0:
            pts += 12; reasons.append("Below book value")
        elif pb < 1.5:
            pts += 8
        elif pb < 2.5:
            pts += 4

    # ROE (10 pts)
    roe = _roe_pct(stock.return_on_equity)
    if roe is not None:
        if roe > 15:
            pts += 10
        elif roe > 8:
            pts += 6
        elif roe > 0:
            pts += 3
        else:
            pts -= 8

    # Analyst (8 pts)
    p, r = _score_analyst_rec(stock.recommendation_mean, 8)
    pts += p; reasons.extend(r)

    # Upside (8 pts)
    p, r = _score_upside(stock, 8, high_thresh=0.35, low_thresh=0.20)
    pts += p; reasons.extend(r)

    p, r = _score_universal(stock, "materials")
    pts += p; reasons.extend(r)

    return pts, reasons


# =====================================================================
# 10. Utilities
# =====================================================================

def _score_utilities(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []

    # Forward P/E (30 pts — primary metric for bond-like utilities)
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 12:
            pts += 30; reasons.append("Very cheap P/E for utility")
        elif fpe < 15:
            pts += 22
        elif fpe < 18:
            pts += 12
        elif fpe < 22:
            pts += 5
        avg_fpe = _safe_avg(avg, "avg_forward_pe")
        if avg_fpe and avg_fpe > 0 and fpe < avg_fpe * 0.85:
            pts += 10; reasons.append("Cheap vs sector P/E")

    # Dividend yield (25 pts)
    dy = stock.dividend_yield
    if dy is not None:
        if dy > 0.06:
            pts += 25; reasons.append(f"High dividend yield {dy*100:.1f}%")
        elif dy > 0.04:
            pts += 18
        elif dy > 0.03:
            pts += 10
        elif dy > 0.02:
            pts += 4
        elif dy <= 0:
            pts -= 15; reasons.append("No dividend — unusual for utility")

    # P/B (15 pts)
    pb = stock.price_to_book
    if pb is not None and pb > 0:
        if pb < 1.0:
            pts += 15; reasons.append("Below book value")
        elif pb < 1.5:
            pts += 10
        elif pb < 2.0:
            pts += 5
        elif pb < 2.5:
            pts += 2

    # EV/EBITDA (12 pts)
    ev = stock.ev_to_ebitda
    if ev is not None and ev > 0:
        if ev < 6:
            pts += 12
        elif ev < 9:
            pts += 8
        elif ev < 12:
            pts += 4

    # ROE (8 pts)
    roe = _roe_pct(stock.return_on_equity)
    if roe is not None:
        if roe > 12:
            pts += 8
        elif roe > 8:
            pts += 5
        elif roe > 0:
            pts += 2

    # Analyst (10 pts)
    p, r = _score_analyst_rec(stock.recommendation_mean, 10)
    pts += p; reasons.extend(r)

    # FCF: WEIGHT 0 for utilities
    # Debt: not penalized — utilities have regulated, stable EBITDA

    p, r = _score_universal(stock, "utilities")
    pts += p; reasons.extend(r)

    return pts, reasons


# =====================================================================
# 11. Default (Technology and unmatched sectors)
# =====================================================================

def _score_default(stock: ScoredStock, avg: Optional[SectorAverages]) -> tuple[int, list[str]]:
    """Original flat scoring logic — works well for tech / general stocks."""
    pts = 0
    reasons: list[str] = []

    # Forward P/E
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 8:
            pts += 28; reasons.append("Very low P/E")
        elif fpe <= 12:
            pts += 20; reasons.append("Low P/E")
        elif fpe <= 18:
            pts += 12
        avg_fpe = _safe_avg(avg, "avg_forward_pe")
        if avg_fpe and avg_fpe > 0:
            if fpe < avg_fpe * 0.70:
                pts += 10; reasons.append("Cheap vs sector")
            elif fpe < avg_fpe * 0.85:
                pts += 5

    # Price/Book
    pb = stock.price_to_book
    if pb is not None and pb > 0:
        if pb < 1.2:
            pts += 18; reasons.append("Near book value")
        elif pb <= 2:
            pts += 12
        elif pb <= 3:
            pts += 6

    # EV/EBITDA
    ev = stock.ev_to_ebitda
    if ev is not None and ev > 0:
        if ev < 6:
            pts += 18; reasons.append("Low EV/EBITDA")
        elif ev <= 9:
            pts += 12; reasons.append("Reasonable EV/EBITDA")
        elif ev <= 12:
            pts += 6
    p, r = _penalty_negative_ev_ebitda(ev)
    pts += p; reasons.extend(r)

    # ROE
    roe = _roe_pct(stock.return_on_equity)
    if roe is not None:
        if roe > 20:
            pts += 14; reasons.append("Strong ROE")
        elif roe > 12:
            pts += 8
        elif roe > 0:
            pts += 3
        else:
            pts -= 8; reasons.append("Neg ROE")

    # FCF yield
    p, r = _score_fcf_yield(stock, 12)
    pts += p; reasons.extend(r)

    # Analyst
    p, r = _score_analyst_rec(stock.recommendation_mean, 12)
    pts += p; reasons.extend(r)

    # Upside
    p, r = _score_upside(stock, 10)
    pts += p; reasons.extend(r)

    # Debt penalty
    de = stock.debt_to_equity
    if de is not None:
        if de > 300:
            pts -= 8
        elif de > 150:
            pts -= 3

    p, r = _score_universal(stock, "default")
    pts += p; reasons.extend(r)

    return pts, reasons


# =====================================================================
# Dispatch table
# =====================================================================

_SCORERS = {
    "financial": _score_financial,
    "reit": _score_reit,
    "energy": _score_energy,
    "healthcare": _score_healthcare,
    "staples": _score_staples,
    "cyclical": _score_cyclical,
    "industrial": _score_industrial,
    "comms": _score_comms,
    "materials": _score_materials,
    "utilities": _score_utilities,
    "default": _score_default,
}

_SECTOR_TYPE_LABELS = {
    "financial": "Financial model (P/B + ROE + ROA)",
    "reit": "REIT model (Div yield + NAV + EV/EBITDA)",
    "energy": "Energy model (EV/EBITDA + P/CF + FCF)",
    "healthcare": "Healthcare model (EV/EBITDA + Fwd P/E + FCF)",
    "staples": "Staples model (EV/EBITDA + Fwd P/E + Div yield)",
    "cyclical": "Cyclical model (Normalized P/E + P/B + EV/EBITDA)",
    "industrial": "Industrial model (EV/EBITDA + Fwd P/E + FCF)",
    "comms": "Comms model (EV/EBITDA + FCF + Fwd P/E)",
    "materials": "Materials model (EV/EBITDA + FCF + P/B)",
    "utilities": "Utilities model (Fwd P/E + Div yield + P/B)",
    "default": "Default model (Fwd P/E + P/B + EV/EBITDA + ROE)",
}


# =====================================================================
# Public API — drop-in replacement for the old compute_score
# =====================================================================

def compute_score(stock: ScoredStock, sector_avg: Optional[SectorAverages]) -> ScoreBreakdown:
    """Compute sector-aware value score. Same inputs/outputs as the old scorer."""
    sector_type = detect_sector_type(stock.sector, stock.industry)
    scorer = _SCORERS.get(sector_type, _score_default)
    raw_score, reasons = scorer(stock, sector_avg)

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
