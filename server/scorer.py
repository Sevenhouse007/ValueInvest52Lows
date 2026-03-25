"""Value scoring engine — implements the exact rubric from the spec."""

from __future__ import annotations

from typing import Optional

from server.models import ScoreBreakdown, ScoredStock, SectorAverages


def compute_score(stock: ScoredStock, sector_avg: Optional[SectorAverages]) -> ScoreBreakdown:
    """Compute value score (0-100+) using the defined rubric."""
    points = 0
    reasons: list[str] = []

    # --- Forward P/E ---
    fpe = stock.forward_pe
    if fpe is not None and fpe > 0:
        if fpe < 8:
            points += 28
            reasons.append("Very low P/E")
        elif fpe <= 12:
            points += 20
            reasons.append("Low P/E")
        elif fpe <= 18:
            points += 12

        # Sector comparison bonus
        if sector_avg and sector_avg.avg_forward_pe and sector_avg.avg_forward_pe > 0:
            if fpe < sector_avg.avg_forward_pe * 0.70:
                points += 10
                reasons.append("Cheap vs sector")
            elif fpe < sector_avg.avg_forward_pe * 0.85:
                points += 5

    # --- Price/Book ---
    pb = stock.price_to_book
    if pb is not None and pb > 0:
        if pb < 1.2:
            points += 18
            reasons.append("Near book value")
        elif pb <= 2:
            points += 12
        elif pb <= 3:
            points += 6

    # --- EV/EBITDA ---
    ev = stock.ev_to_ebitda
    if ev is not None and ev > 0:
        if ev < 6:
            points += 18
            reasons.append("Low EV/EBITDA")
        elif ev <= 9:
            points += 12
            reasons.append("Reasonable EV/EBITDA")
        elif ev <= 12:
            points += 6

    # --- ROE ---
    roe = stock.return_on_equity
    if roe is not None:
        roe_pct = roe * 100 if abs(roe) < 1 else roe  # handle both decimal and pct
        if roe_pct > 20:
            points += 14
            reasons.append("Strong ROE")
        elif roe_pct > 12:
            points += 8
        elif roe_pct > 0:
            points += 3
        else:
            points -= 8
            reasons.append("Neg ROE")

    # --- Free Cash Flow ---
    fcf = stock.free_cash_flow
    if fcf is not None:
        if fcf > 0:
            points += 10
            reasons.append("Positive FCF")
        else:
            points -= 5

    # --- Analyst Recommendation ---
    rec = stock.recommendation_mean
    if rec is not None and rec > 0:
        if rec < 1.8:
            points += 12
            reasons.append("Analyst: Strong Buy")
        elif rec < 2.3:
            points += 7

    # --- Upside ---
    if stock.target_mean_price and stock.price and stock.price > 0:
        upside = (stock.target_mean_price - stock.price) / stock.price
        stock.upside_percent = round(upside * 100, 1)
        if upside > 0.40:
            points += 10
            reasons.append(f"{stock.upside_percent:.0f}% upside")
        elif upside > 0.20:
            points += 6
            reasons.append(f"{stock.upside_percent:.0f}% upside")

    # --- Debt/Equity penalty ---
    de = stock.debt_to_equity
    if de is not None:
        if de > 300:
            points -= 8
        elif de > 150:
            points -= 3

    total = max(0, points)
    if total >= 70:
        tier = "Strong Value"
    elif total >= 40:
        tier = "Moderate"
    else:
        tier = "Limited signal"

    return ScoreBreakdown(total=total, tier=tier, reasons=reasons)
