"""Damodaran NYU Stern sector medians — January 2026 data.

Source: pages.stern.nyu.edu/~adamodar/
Update this file annually each January.

Used to blend with live blue-chip benchmarks:
  blended = 60% blue-chip (current market) + 40% Damodaran (broad sector median)
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)

LAST_UPDATED = "2026-01-15"

# Threshold for emitting a staleness warning. The Damodaran dataset refreshes
# every January; 400 days gives roughly a one-month grace period after the
# expected refresh window before we start nagging.
_STALENESS_THRESHOLD_DAYS = 400


def _check_staleness() -> None:
    """Emit a warning if SECTOR_MEDIANS / SECTOR_WACC are overdue for refresh."""
    try:
        last = date.fromisoformat(LAST_UPDATED)
    except ValueError:
        logger.warning("Damodaran LAST_UPDATED=%r is not ISO-8601; cannot check staleness", LAST_UPDATED)
        return
    age_days = (date.today() - last).days
    if age_days > _STALENESS_THRESHOLD_DAYS:
        logger.warning(
            "Damodaran sector data is %d days old (last updated %s). "
            "Refresh server/damodaran_benchmarks.py from pages.stern.nyu.edu/~adamodar/.",
            age_days, LAST_UPDATED,
        )


_check_staleness()

# Sector WACC estimates (Damodaran Jan 2026)
SECTOR_WACC = {
    "Technology": 0.099,
    "Financial Services": 0.088,
    "Healthcare": 0.082,
    "Consumer Cyclical": 0.087,
    "Consumer Defensive": 0.072,
    "Energy": 0.091,
    "Industrials": 0.083,
    "Basic Materials": 0.085,
    "Real Estate": 0.076,
    "Utilities": 0.065,
    "Communication Services": 0.091,
}

SECTOR_MEDIANS = {
    # Each sector entry: pe, pb, ev_ebitda, roe (decimal), ps, de (% — total debt / equity), div_yield (decimal)
    # Damodaran "Total Market" / sector pages, Jan 2026 dataset.
    "Financial Services": {"pe": 12.5, "pb": 1.4, "ev_ebitda": None, "roe": 0.112, "ps": 2.6, "de": 105.0, "div_yield": 0.027},
    "Real Estate":        {"pe": 26.2, "pb": 1.6, "ev_ebitda": 16.4, "roe": 0.068, "ps": 5.8, "de": 95.0,  "div_yield": 0.041},
    "Energy":             {"pe": 11.8, "pb": 1.9, "ev_ebitda": 5.8,  "roe": 0.142, "ps": 1.4, "de": 55.0,  "div_yield": 0.040},
    "Healthcare":         {"pe": 17.5, "pb": 3.8, "ev_ebitda": 14.3, "roe": 0.141, "ps": 2.5, "de": 60.0,  "div_yield": 0.018},
    "Consumer Defensive": {"pe": 14.7, "pb": 2.8, "ev_ebitda": 13.1, "roe": 0.138, "ps": 1.3, "de": 70.0,  "div_yield": 0.025},
    "Consumer Cyclical":  {"pe": 14.7, "pb": 3.6, "ev_ebitda": 13.9, "roe": 0.209, "ps": 1.5, "de": 80.0,  "div_yield": 0.018},
    "Industrials":        {"pe": 21.8, "pb": 4.0, "ev_ebitda": 16.1, "roe": 0.132, "ps": 1.9, "de": 65.0,  "div_yield": 0.018},
    "Communication Services": {"pe": 15.2, "pb": 3.2, "ev_ebitda": 9.8, "roe": 0.148, "ps": 2.2, "de": 70.0, "div_yield": 0.022},
    "Basic Materials":    {"pe": 18.2, "pb": 2.8, "ev_ebitda": 10.9, "roe": 0.104, "ps": 1.6, "de": 50.0,  "div_yield": 0.025},
    "Utilities":          {"pe": 17.9, "pb": 1.7, "ev_ebitda": 10.2, "roe": 0.096, "ps": 2.4, "de": 145.0, "div_yield": 0.035},
    "Technology":         {"pe": 28.4, "pb": 6.8, "ev_ebitda": 18.6, "roe": 0.221, "ps": 6.0, "de": 40.0,  "div_yield": 0.012},
}


def blend_with_damodaran(sector: str, bluechip_avg: dict) -> dict:
    """Blend 60% blue-chip + 40% Damodaran for a sector.

    Args:
        sector: Yahoo Finance sector name
        bluechip_avg: dict with avg_forward_pe, avg_price_to_book, avg_ev_to_ebitda, avg_roe

    Returns:
        Same dict structure with blended values
    """
    dam = SECTOR_MEDIANS.get(sector)
    if not dam:
        return bluechip_avg

    def _blend(bc_val, dam_val):
        if bc_val is not None and dam_val is not None:
            return round(bc_val * 0.6 + dam_val * 0.4, 2)
        return bc_val  # fall back to blue-chip only

    return {
        "avg_forward_pe": _blend(bluechip_avg.get("avg_forward_pe"), dam.get("pe")),
        "avg_price_to_book": _blend(bluechip_avg.get("avg_price_to_book"), dam.get("pb")),
        "avg_ev_to_ebitda": _blend(bluechip_avg.get("avg_ev_to_ebitda"), dam.get("ev_ebitda")),
        "avg_roe": _blend(bluechip_avg.get("avg_roe"), dam.get("roe")),
    }
