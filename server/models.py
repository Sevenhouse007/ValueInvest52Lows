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
    return_on_assets: Optional[float] = None
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None
    current_ratio: Optional[float] = None
    recommendation_mean: Optional[float] = None
    target_mean_price: Optional[float] = None
    price_to_sales: Optional[float] = None
    dividend_yield: Optional[float] = None
    operating_cashflow: Optional[float] = None
    enterprise_value: Optional[float] = None
    roic: Optional[float] = None
    ebit: Optional[float] = None
    ebitda: Optional[float] = None
    total_debt: Optional[float] = None
    total_cash: Optional[float] = None
    interest_expense: Optional[float] = None
    payout_ratio: Optional[float] = None
    total_assets: Optional[float] = None
    total_current_assets: Optional[float] = None
    total_current_liabilities: Optional[float] = None
    total_liabilities: Optional[float] = None
    retained_earnings: Optional[float] = None
    revenue_acceleration: Optional[float] = None  # current YoY growth - prior YoY growth
    ebitda_growth: Optional[float] = None
    net_debt_ebitda: Optional[float] = None
    interest_coverage: Optional[float] = None
    altman_z_score: Optional[float] = None
    short_percent_of_float: Optional[float] = None
    insider_buy_count: int = 0
    insider_sell_count: int = 0
    insider_net_shares: Optional[int] = None
    piotroski_f_score: Optional[int] = None
    piotroski_details: list[str] = Field(default_factory=list)
    gross_margin_change: Optional[float] = None
    buyback_yield: Optional[float] = None
    # Change A: Accruals quality
    accruals_ratio: Optional[float] = None
    # Change B: Earnings estimate revision
    avg_eps_surprise: Optional[float] = None
    eps_surprise_trend: Optional[float] = None
    # Change C: Historical mean reversion
    five_year_avg_div_yield: Optional[float] = None
    trailing_pe: Optional[float] = None
    # New signals batch
    gp_to_assets: Optional[float] = None  # Gross Profit / Total Assets
    gross_profit: Optional[float] = None
    ev_gross_profit: Optional[float] = None  # EV / Gross Profit
    ffo: Optional[float] = None  # Funds From Operations (REITs)
    p_ffo: Optional[float] = None  # Price / FFO
    beneish_m_score: Optional[float] = None
    asset_growth: Optional[float] = None  # YoY total asset growth
    held_pct_institutions: Optional[float] = None
    total_assets_prior: Optional[float] = None
    depreciation_amortization: Optional[float] = None
    current_long_term_debt: Optional[float] = None  # current portion of LTD
    total_long_term_debt: Optional[float] = None
    ncav: Optional[float] = None  # Net Current Asset Value
    ncav_per_share: Optional[float] = None
    shares_outstanding: Optional[float] = None
    shareholder_yield: Optional[float] = None
    debt_maturity_ratio: Optional[float] = None
    sector: str = ""
    industry: str = ""
    country: str = ""


class SectorAverages(BaseModel):
    """Average metrics for a sector."""
    sector: str
    avg_forward_pe: Optional[float] = None
    avg_price_to_book: Optional[float] = None
    avg_ev_to_ebitda: Optional[float] = None
    avg_roe: Optional[float] = None
    avg_dividend_yield: Optional[float] = None
    avg_debt_to_equity: Optional[float] = None
    avg_price_to_sales: Optional[float] = None
    stock_count: int = 0


class ScoreBreakdown(BaseModel):
    """Detailed scoring breakdown."""
    total: int = 0
    tier: str = "Limited signal"
    reasons: list[str] = Field(default_factory=list)
    sector_type: str = "default"


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
    return_on_assets: Optional[float] = None
    free_cash_flow: Optional[float] = None
    operating_cashflow: Optional[float] = None
    enterprise_value: Optional[float] = None
    roic: Optional[float] = None
    ebit: Optional[float] = None
    ebitda: Optional[float] = None
    total_debt: Optional[float] = None
    total_cash: Optional[float] = None
    interest_expense: Optional[float] = None
    payout_ratio: Optional[float] = None
    total_assets: Optional[float] = None
    total_current_assets: Optional[float] = None
    total_current_liabilities: Optional[float] = None
    total_liabilities: Optional[float] = None
    retained_earnings: Optional[float] = None
    revenue_acceleration: Optional[float] = None
    ebitda_growth: Optional[float] = None
    net_debt_ebitda: Optional[float] = None
    interest_coverage: Optional[float] = None
    altman_z_score: Optional[float] = None
    recommendation_mean: Optional[float] = None
    target_mean_price: Optional[float] = None
    price_to_sales: Optional[float] = None
    dividend_yield: Optional[float] = None
    short_percent_of_float: Optional[float] = None
    insider_buy_count: int = 0
    insider_sell_count: int = 0
    insider_net_shares: Optional[int] = None
    piotroski_f_score: Optional[int] = None
    piotroski_details: list[str] = Field(default_factory=list)
    gross_margin_change: Optional[float] = None
    buyback_yield: Optional[float] = None
    accruals_ratio: Optional[float] = None
    avg_eps_surprise: Optional[float] = None
    eps_surprise_trend: Optional[float] = None
    five_year_avg_div_yield: Optional[float] = None
    trailing_pe: Optional[float] = None
    gp_to_assets: Optional[float] = None
    gross_profit: Optional[float] = None
    ev_gross_profit: Optional[float] = None
    ffo: Optional[float] = None
    p_ffo: Optional[float] = None
    beneish_m_score: Optional[float] = None
    asset_growth: Optional[float] = None
    held_pct_institutions: Optional[float] = None
    total_assets_prior: Optional[float] = None
    depreciation_amortization: Optional[float] = None
    current_long_term_debt: Optional[float] = None
    total_long_term_debt: Optional[float] = None
    ncav: Optional[float] = None
    ncav_per_share: Optional[float] = None
    shares_outstanding: Optional[float] = None
    shareholder_yield: Optional[float] = None
    debt_maturity_ratio: Optional[float] = None
    price_momentum_3m: Optional[float] = None
    price_momentum_12m: Optional[float] = None
    relative_momentum: Optional[float] = None
    country: str = ""
    debt_to_equity: Optional[float] = None
    ev_to_revenue: Optional[float] = None
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None
    current_ratio: Optional[float] = None
    # Sector averages (from 52W-low scan stocks)
    sector_avg_fpe: Optional[float] = None
    sector_avg_pb: Optional[float] = None
    sector_avg_ev_ebitda: Optional[float] = None
    sector_avg_roe: Optional[float] = None
    # Market sector averages (from blue-chip benchmarks)
    market_avg_fpe: Optional[float] = None
    market_avg_pb: Optional[float] = None
    market_avg_ev_ebitda: Optional[float] = None
    market_avg_roe: Optional[float] = None
    market_avg_div_yield: Optional[float] = None
    market_avg_debt_equity: Optional[float] = None
    market_avg_ps: Optional[float] = None
    # Industry averages (narrow peer group, used for scoring)
    industry_avg_fpe: Optional[float] = None
    industry_avg_pb: Optional[float] = None
    industry_avg_ev_ebitda: Optional[float] = None
    industry_avg_roe: Optional[float] = None
    industry_peer_count: int = 0
    # Value score (cheapness-focused)
    value_score: int = 0
    score_tier: str = "Limited signal"
    score_reasons: list[str] = Field(default_factory=list)
    sector_type: str = "default"
    # Quality score (business quality at a fair price)
    quality_score: int = 0
    quality_tier: str = "Limited"
    quality_reasons: list[str] = Field(default_factory=list)
    # Upside
    upside_percent: Optional[float] = None


class ScanResult(BaseModel):
    """A complete scan result."""
    scan_date: str
    scanned_at: str
    total_stocks: int = 0
    stocks: list[ScoredStock] = Field(default_factory=list)
    sector_averages: dict[str, SectorAverages] = Field(default_factory=dict)
    market_sector_averages: dict[str, SectorAverages] = Field(default_factory=dict)


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
