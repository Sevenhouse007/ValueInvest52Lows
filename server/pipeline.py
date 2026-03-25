"""Data pipeline — orchestrates the 6-step fetch, filter, enrich, score flow."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from server.config import (
    OUTLIER_EV_EBITDA_MAX,
    OUTLIER_FPE_MAX,
    OUTLIER_PB_MAX,
)
from server.models import ScanResult, ScoredStock, SectorAverages, StockFundamentals, StockQuote
from server.scorer import compute_score
from server.yahoo_client import YahooClient

logger = logging.getLogger(__name__)


def _safe_raw(obj: Optional[dict], key: str) -> Optional[float]:
    """Extract .raw value from Yahoo's {raw, fmt} wrappers."""
    if obj is None:
        return None
    val = obj.get(key)
    if val is None:
        return None
    if isinstance(val, dict):
        return val.get("raw")
    if isinstance(val, (int, float)):
        return float(val)
    return None


def parse_screener_quote(q: dict) -> StockQuote:
    """Step 1: Parse a single quote from screener response."""
    return StockQuote(
        symbol=q.get("symbol", ""),
        short_name=q.get("shortName", ""),
        price=q.get("regularMarketPrice", 0),
        market_cap=q.get("marketCap", 0),
        change_percent=q.get("regularMarketChangePercent", 0),
        trailing_pe=q.get("trailingPE"),
        fifty_two_week_low=q.get("fiftyTwoWeekLow", 0),
        fifty_two_week_high=q.get("fiftyTwoWeekHigh", 0),
    )



def parse_fundamentals(symbol: str, data: dict) -> StockFundamentals:
    """Step 4: Parse quoteSummary response into StockFundamentals."""
    stats = data.get("defaultKeyStatistics", {})
    fin = data.get("financialData", {})
    summary = data.get("summaryDetail", {})
    profile = data.get("assetProfile", {})

    return StockFundamentals(
        symbol=symbol,
        forward_pe=_safe_raw(stats, "forwardPE"),
        price_to_book=_safe_raw(stats, "priceToBook"),
        ev_to_ebitda=_safe_raw(stats, "enterpriseToEbitda"),
        ev_to_revenue=_safe_raw(stats, "enterpriseToRevenue"),
        debt_to_equity=_safe_raw(fin, "debtToEquity"),
        free_cash_flow=_safe_raw(fin, "freeCashflow"),
        return_on_equity=_safe_raw(fin, "returnOnEquity"),
        revenue_growth=_safe_raw(fin, "revenueGrowth"),
        earnings_growth=_safe_raw(fin, "earningsGrowth"),
        current_ratio=_safe_raw(fin, "currentRatio"),
        recommendation_mean=_safe_raw(fin, "recommendationMean"),
        target_mean_price=_safe_raw(fin, "targetMeanPrice"),
        price_to_sales=_safe_raw(summary, "priceToSalesTrailing12Months"),
        sector=profile.get("sector", ""),
        industry=profile.get("industry", ""),
    )



def compute_sector_averages(stocks: list[ScoredStock]) -> dict[str, SectorAverages]:
    """Step 5: Compute sector averages with outlier exclusion."""
    by_sector: dict[str, list[ScoredStock]] = defaultdict(list)
    for s in stocks:
        if s.sector:
            by_sector[s.sector].append(s)

    averages: dict[str, SectorAverages] = {}
    for sector, sector_stocks in by_sector.items():
        fpes = [
            s.forward_pe for s in sector_stocks
            if s.forward_pe is not None and 0 < s.forward_pe < OUTLIER_FPE_MAX
        ]
        pbs = [
            s.price_to_book for s in sector_stocks
            if s.price_to_book is not None and 0 < s.price_to_book < OUTLIER_PB_MAX
        ]
        evs = [
            s.ev_to_ebitda for s in sector_stocks
            if s.ev_to_ebitda is not None and 0 < s.ev_to_ebitda < OUTLIER_EV_EBITDA_MAX
        ]
        roes = [
            s.return_on_equity for s in sector_stocks
            if s.return_on_equity is not None
        ]

        averages[sector] = SectorAverages(
            sector=sector,
            avg_forward_pe=_avg(fpes),
            avg_price_to_book=_avg(pbs),
            avg_ev_to_ebitda=_avg(evs),
            avg_roe=_avg(roes),
            stock_count=len(sector_stocks),
        )
    return averages


def _avg(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def merge_quote_and_fundamentals(
    quote: StockQuote, fundamentals: Optional[StockFundamentals]
) -> ScoredStock:
    """Combine screener quote with enriched fundamentals into a ScoredStock."""
    s = ScoredStock(
        symbol=quote.symbol,
        short_name=quote.short_name,
        price=quote.price,
        market_cap=quote.market_cap,
        change_percent=quote.change_percent,
        fifty_two_week_low=quote.fifty_two_week_low,
        fifty_two_week_high=quote.fifty_two_week_high,
    )
    if fundamentals:
        s.sector = fundamentals.sector
        s.industry = fundamentals.industry
        s.forward_pe = fundamentals.forward_pe
        s.price_to_book = fundamentals.price_to_book
        s.ev_to_ebitda = fundamentals.ev_to_ebitda
        s.return_on_equity = fundamentals.return_on_equity
        s.free_cash_flow = fundamentals.free_cash_flow
        s.recommendation_mean = fundamentals.recommendation_mean
        s.target_mean_price = fundamentals.target_mean_price
        s.price_to_sales = fundamentals.price_to_sales
        s.debt_to_equity = fundamentals.debt_to_equity
        s.ev_to_revenue = fundamentals.ev_to_revenue
        s.revenue_growth = fundamentals.revenue_growth
        s.earnings_growth = fundamentals.earnings_growth
        s.current_ratio = fundamentals.current_ratio
    return s


async def run_pipeline(client: Optional[YahooClient] = None) -> ScanResult:
    """Execute the full 6-step pipeline and return scored results."""
    own_client = client is None
    if own_client:
        client = YahooClient()

    try:
        # Step 1: Fetch screener
        logger.info("Step 1: Fetching 52-week low screener...")
        raw_quotes = await client.fetch_screener()
        quotes = [parse_screener_quote(q) for q in raw_quotes]
        logger.info(f"Parsed {len(quotes)} quotes")

        # Steps 3-4: Fetch fundamentals (crumb handled inside client)
        logger.info(f"Steps 3-4: Fetching fundamentals for {len(quotes)} stocks...")
        symbols = [q.symbol for q in quotes]
        fundamentals_raw = await client.fetch_fundamentals_batch(symbols)

        # Parse and merge
        fundamentals_map: dict[str, StockFundamentals] = {}
        for sym, data in fundamentals_raw.items():
            fundamentals_map[sym] = parse_fundamentals(sym, data)

        quote_map = {q.symbol: q for q in quotes}
        stocks: list[ScoredStock] = []
        for sym in symbols:
            fund = fundamentals_map.get(sym)
            quote = quote_map[sym]
            stocks.append(merge_quote_and_fundamentals(quote, fund))

        # Step 5: Compute sector averages
        logger.info("Step 5: Computing sector averages...")
        sector_averages = compute_sector_averages(stocks)

        # Attach sector averages to each stock
        for s in stocks:
            avg = sector_averages.get(s.sector)
            if avg:
                s.sector_avg_fpe = avg.avg_forward_pe
                s.sector_avg_pb = avg.avg_price_to_book
                s.sector_avg_ev_ebitda = avg.avg_ev_to_ebitda
                s.sector_avg_roe = avg.avg_roe

        # Step 6: Score each stock
        logger.info("Step 6: Scoring stocks...")
        for s in stocks:
            avg = sector_averages.get(s.sector)
            breakdown = compute_score(s, avg)
            s.value_score = breakdown.total
            s.score_tier = breakdown.tier
            s.score_reasons = breakdown.reasons

        # Sort by score descending
        stocks.sort(key=lambda x: x.value_score, reverse=True)

        now = datetime.now(timezone.utc)
        result = ScanResult(
            scan_date=now.strftime("%Y-%m-%d"),
            scanned_at=now.isoformat(),
            total_stocks=len(stocks),
            stocks=stocks,
            sector_averages=sector_averages,
        )

        logger.info(
            f"Pipeline complete: {len(stocks)} stocks scored. "
            f"Top score: {stocks[0].value_score if stocks else 0}"
        )
        return result

    finally:
        if own_client:
            await client.close()
