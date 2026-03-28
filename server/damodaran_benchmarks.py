"""Damodaran NYU Stern sector medians — January 2026 data.

Source: pages.stern.nyu.edu/~adamodar/
Update this file annually each January.

Used to blend with live blue-chip benchmarks:
  blended = 60% blue-chip (current market) + 40% Damodaran (broad sector median)
"""

LAST_UPDATED = "2026-01-15"

SECTOR_MEDIANS = {
    "Financial Services": {"pe": 12.5, "pb": 1.4, "ev_ebitda": None, "roe": 0.112},
    "Real Estate":        {"pe": 26.2, "pb": 1.6, "ev_ebitda": 16.4, "roe": 0.068},
    "Energy":             {"pe": 11.8, "pb": 1.9, "ev_ebitda": 5.8,  "roe": 0.142},
    "Healthcare":         {"pe": 17.5, "pb": 3.8, "ev_ebitda": 14.3, "roe": 0.141},
    "Consumer Defensive": {"pe": 14.7, "pb": 2.8, "ev_ebitda": 13.1, "roe": 0.138},
    "Consumer Cyclical":  {"pe": 14.7, "pb": 3.6, "ev_ebitda": 13.9, "roe": 0.209},
    "Industrials":        {"pe": 21.8, "pb": 4.0, "ev_ebitda": 16.1, "roe": 0.132},
    "Communication Services": {"pe": 15.2, "pb": 3.2, "ev_ebitda": 9.8, "roe": 0.148},
    "Basic Materials":    {"pe": 18.2, "pb": 2.8, "ev_ebitda": 10.9, "roe": 0.104},
    "Utilities":          {"pe": 17.9, "pb": 1.7, "ev_ebitda": 10.2, "roe": 0.096},
    "Technology":         {"pe": 28.4, "pb": 6.8, "ev_ebitda": 18.6, "roe": 0.221},
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
