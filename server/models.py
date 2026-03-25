"""Pydantic models for the 52W Low Value Scanner."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class StockQuote(BaseModel):
    """Raw quote from the Yahoo screener."""
    symbol: str
    short_name: str = ""
    price: float = 0.0
    market_cap: float = 0.0
    change_percent: float = 0.0
    trailing_pe: Optional[float] = None
    fifty_two_week_low: float = 0.0
    fifty_two_week_high: float = 0.0


class StockFundamentals(BaseModel):
    """Enriched fundamentals from quoteSummary."""
    symbol: str
    forward_pe: Optional[float] = None
    price_to_book: Optional[float] = None
    ev_to_ebitda: Optional[float] = None
    ev_to_revenue: Optional[float] = None
    debt_to_equity: Optional[float] = None
    free_cash_flow: Optional[float] = None
    return_on_equity: Optional[float] = None
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None
    current_ratio: Optional[float] = None
    recommendation_mean: Optional[float] = None
    target_mean_price: Optional[float] = None
    price_to_sales: Optional[float] = None
    sector: str = ""
    industry: str = ""


class SectorAverages(BaseModel):
    """Average metrics for a sector."""
    sector: str
    avg_forward_pe: Optional[float] = None
    avg_price_to_book: Optional[float] = None
    avg_ev_to_ebitda: Optional[float] = None
    avg_roe: Optional[float] = None
    stock_count: int = 0


class ScoreBreakdown(BaseModel):
    """Detailed scoring breakdown."""
    total: int = 0
    tier: str = "Limited signal"
    reasons: list[str] = Field(default_factory=list)


class ScoredStock(BaseModel):
    """Final scored stock combining all data."""
    symbol: str
    short_name: str = ""
    price: float = 0.0
    market_cap: float = 0.0
    change_percent: float = 0.0
    fifty_two_week_low: float = 0.0
    fifty_two_week_high: float = 0.0
    sector: str = ""
    industry: str = ""
    forward_pe: Optional[float] = None
    price_to_book: Optional[float] = None
    ev_to_ebitda: Optional[float] = None
    return_on_equity: Optional[float] = None
    free_cash_flow: Optional[float] = None
    recommendation_mean: Optional[float] = None
    target_mean_price: Optional[float] = None
    price_to_sales: Optional[float] = None
    debt_to_equity: Optional[float] = None
    ev_to_revenue: Optional[float] = None
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None
    current_ratio: Optional[float] = None
    # Sector averages
    sector_avg_fpe: Optional[float] = None
    sector_avg_pb: Optional[float] = None
    sector_avg_ev_ebitda: Optional[float] = None
    sector_avg_roe: Optional[float] = None
    # Score
    value_score: int = 0
    score_tier: str = "Limited signal"
    score_reasons: list[str] = Field(default_factory=list)
    # Upside
    upside_percent: Optional[float] = None


class ScanResult(BaseModel):
    """A complete scan result."""
    scan_date: str
    scanned_at: str
    total_stocks: int = 0
    stocks: list[ScoredStock] = Field(default_factory=list)
    sector_averages: dict[str, SectorAverages] = Field(default_factory=dict)


class ScanSummary(BaseModel):
    """Summary cards data."""
    total_scanned: int = 0
    strong_value_count: int = 0
    average_score: float = 0.0
    top_sector: str = ""
    top_sector_count: int = 0


class ScanHistoryEntry(BaseModel):
    """A single entry in scan history."""
    scan_date: str
    scanned_at: str
    total_stocks: int
