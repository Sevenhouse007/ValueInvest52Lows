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
    SECTOR_BENCHMARK_TICKERS,
)
from server.models import ScanResult, ScoredStock, SectorAverages, StockFundamentals, StockQuote
from server.scorer import compute_quality_score, compute_score
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



def parse_quote_from_summary(symbol: str, data: dict) -> StockQuote:
    """Build a StockQuote from quoteSummary data (for ad-hoc lookup)."""
    summary = data.get("summaryDetail", {})
    price_data = data.get("price", {})
    # shortName can be in price module or quoteType
    short_name = ""
    if isinstance(price_data.get("shortName"), str):
        short_name = price_data["shortName"]
    elif isinstance(price_data.get("longName"), str):
        short_name = price_data["longName"]

    return StockQuote(
        symbol=symbol,
        short_name=short_name,
        price=_safe_raw(summary, "regularMarketPrice") or _safe_raw(price_data, "regularMarketPrice") or 0,
        market_cap=_safe_raw(summary, "marketCap") or _safe_raw(price_data, "marketCap") or 0,
        change_percent=_safe_raw(summary, "regularMarketChangePercent") or 0,
        fifty_two_week_low=_safe_raw(summary, "fiftyTwoWeekLow") or 0,
        fifty_two_week_high=_safe_raw(summary, "fiftyTwoWeekHigh") or 0,
    )


def parse_fundamentals(symbol: str, data: dict) -> StockFundamentals:
    """Step 4: Parse quoteSummary response into StockFundamentals."""
    stats = data.get("defaultKeyStatistics", {})
    fin = data.get("financialData", {})
    summary = data.get("summaryDetail", {})
    profile = data.get("assetProfile", {})

    # Insider transactions (last 6 months)
    buy_count, sell_count, net_shares = _parse_insider_transactions(data)

    # Piotroski F-Score
    f_score, f_details = _compute_piotroski(data, fin)

    # Gross margin change YoY
    gm_change = _compute_gross_margin_change(data)

    # Buyback yield (share count change)
    bb_yield = _compute_buyback_yield(stats)

    # Enterprise value and ROIC
    ev = _safe_raw(stats, "enterpriseValue")
    ocf = _safe_raw(fin, "operatingCashflow")
    roic = None
    if ev and ev > 0 and ocf is not None:
        # ROIC approximated as Operating Cashflow / Enterprise Value
        roic = round(ocf / ev, 4)

    return StockFundamentals(
        symbol=symbol,
        forward_pe=_safe_raw(stats, "forwardPE"),
        price_to_book=_safe_raw(stats, "priceToBook"),
        ev_to_ebitda=_safe_raw(stats, "enterpriseToEbitda"),
        ev_to_revenue=_safe_raw(stats, "enterpriseToRevenue"),
        debt_to_equity=_safe_raw(fin, "debtToEquity"),
        free_cash_flow=_safe_raw(fin, "freeCashflow"),
        operating_cashflow=_safe_raw(fin, "operatingCashflow"),
        enterprise_value=ev,
        roic=roic,
        return_on_equity=_safe_raw(fin, "returnOnEquity"),
        return_on_assets=_safe_raw(fin, "returnOnAssets"),
        revenue_growth=_safe_raw(fin, "revenueGrowth"),
        earnings_growth=_safe_raw(fin, "earningsGrowth"),
        current_ratio=_safe_raw(fin, "currentRatio"),
        recommendation_mean=_safe_raw(fin, "recommendationMean"),
        target_mean_price=_safe_raw(fin, "targetMeanPrice"),
        price_to_sales=_safe_raw(summary, "priceToSalesTrailing12Months"),
        dividend_yield=_safe_raw(summary, "dividendYield"),
        short_percent_of_float=_safe_raw(stats, "shortPercentOfFloat"),
        insider_buy_count=buy_count,
        insider_sell_count=sell_count,
        insider_net_shares=net_shares,
        piotroski_f_score=f_score,
        piotroski_details=f_details,
        gross_margin_change=gm_change,
        buyback_yield=bb_yield,
        sector=profile.get("sector", ""),
        industry=profile.get("industry", ""),
        country=profile.get("country", ""),
    )


def _parse_insider_transactions(data: dict) -> tuple[int, int, Optional[int]]:
    """Parse insider transactions from last 6 months. Returns (buys, sells, net_shares)."""
    txns = data.get("insiderTransactions", {}).get("transactions", [])
    if not txns:
        return 0, 0, None

    import time
    six_months_ago = time.time() - (180 * 86400)

    buy_count = 0
    sell_count = 0
    net_shares = 0
    for tx in txns:
        start = tx.get("startDate", {})
        ts = start.get("raw", 0) if isinstance(start, dict) else 0
        if ts < six_months_ago:
            continue
        text = (tx.get("transactionText") or "").lower()
        shares = _safe_raw(tx, "shares") or 0
        if "purchase" in text or "buy" in text:
            buy_count += 1
            net_shares += int(shares)
        elif "sale" in text or "sell" in text:
            sell_count += 1
            net_shares -= int(shares)

    return buy_count, sell_count, net_shares if (buy_count + sell_count) > 0 else None


def _compute_gross_margin_change(data: dict) -> Optional[float]:
    """Compute YoY change in gross margin from income statement history."""
    inc_hist = data.get("incomeStatementHistory", {}).get("incomeStatementHistory", [])
    if len(inc_hist) < 2:
        return None
    cur, prev = inc_hist[0], inc_hist[1]
    cur_gp = _safe_raw(cur, "grossProfit")
    cur_rev = _safe_raw(cur, "totalRevenue")
    prev_gp = _safe_raw(prev, "grossProfit")
    prev_rev = _safe_raw(prev, "totalRevenue")
    if not all([cur_gp, cur_rev, prev_gp, prev_rev]) or cur_rev == 0 or prev_rev == 0:
        return None
    cur_margin = cur_gp / cur_rev
    prev_margin = prev_gp / prev_rev
    return round(cur_margin - prev_margin, 4)  # e.g., 0.02 = +2pp improvement


def _compute_buyback_yield(stats: dict) -> Optional[float]:
    """Approximate buyback yield from shares outstanding vs float.

    If shares outstanding is decreasing, the company is buying back shares.
    Uses impliedSharesOutstanding and sharesOutstanding from defaultKeyStatistics.
    Returns positive for buybacks, negative for dilution.
    """
    cur_shares = _safe_raw(stats, "sharesOutstanding")
    float_shares = _safe_raw(stats, "floatShares")
    implied = _safe_raw(stats, "impliedSharesOutstanding")
    # Use implied vs current as a proxy for buyback direction
    if implied and cur_shares and cur_shares > 0:
        change = (cur_shares - implied) / cur_shares
        if abs(change) > 0.001:  # ignore tiny rounding differences
            return round(-change, 4)  # positive = net buyback
    return None


def _compute_relative_momentum(stocks: list) -> None:
    """Compute each stock's drop vs its sector average drop.

    Positive = outperforming (dropped less than peers).
    Negative = underperforming (dropped more than peers).
    """
    sector_drops: dict[str, list[float]] = defaultdict(list)
    for s in stocks:
        if s.price_momentum_12m is not None and s.sector:
            sector_drops[s.sector].append(s.price_momentum_12m)

    sector_avg_drop: dict[str, float] = {}
    for sector, drops in sector_drops.items():
        if drops:
            sector_avg_drop[sector] = sum(drops) / len(drops)

    for s in stocks:
        if s.price_momentum_12m is not None and s.sector in sector_avg_drop:
            # Relative = stock drop - sector avg drop
            # If stock dropped -30% and sector avg is -25%, relative = -5 (underperforming)
            s.relative_momentum = round(s.price_momentum_12m - sector_avg_drop[s.sector], 1)


def _compute_piotroski(data: dict, fin: dict) -> tuple[Optional[int], list[str]]:
    """Compute Piotroski F-Score (0-9).

    Uses income statement history for YoY comparisons and current-period
    financialData for the rest. Yahoo's balance sheet history no longer
    returns detailed fields, so we approximate tests 5-7 from available data.
    """
    inc_hist = data.get("incomeStatementHistory", {}).get("incomeStatementHistory", [])
    stats = data.get("defaultKeyStatistics", {})

    cur_ocf = _safe_raw(fin, "operatingCashflow")
    cur_roa = _safe_raw(fin, "returnOnAssets")
    cur_roe = _safe_raw(fin, "returnOnEquity")
    cur_cr = _safe_raw(fin, "currentRatio")
    cur_de = _safe_raw(fin, "debtToEquity")
    cur_fcf = _safe_raw(fin, "freeCashflow")

    score = 0
    details: list[str] = []
    max_tests = 0  # track how many tests we can actually run

    def g(stmt: dict, key: str) -> Optional[float]:
        return _safe_raw(stmt, key)

    # --- Tests from current-period financialData (always available) ---

    # 1. ROA > 0
    max_tests += 1
    if cur_roa is not None and cur_roa > 0:
        score += 1; details.append("ROA positive")

    # 2. Operating cash flow > 0
    max_tests += 1
    if cur_ocf is not None and cur_ocf > 0:
        score += 1; details.append("OCF positive")

    # 4. CFO > Net Income (quality of earnings)
    if len(inc_hist) >= 1:
        cur_ni = g(inc_hist[0], "netIncome")
        max_tests += 1
        if cur_ocf is not None and cur_ni is not None and cur_ocf > cur_ni:
            score += 1; details.append("CFO > Net Income")

    # --- Tests from income statement YoY comparison ---
    if len(inc_hist) >= 2:
        inc_cur, inc_prev = inc_hist[0], inc_hist[1]
        cur_ni = g(inc_cur, "netIncome")
        prev_ni = g(inc_prev, "netIncome")
        cur_rev = g(inc_cur, "totalRevenue")
        prev_rev = g(inc_prev, "totalRevenue")
        cur_gp = g(inc_cur, "grossProfit")
        prev_gp = g(inc_prev, "grossProfit")

        # 3. ROA improving (approximate: net income growing and positive)
        max_tests += 1
        if cur_ni and prev_ni and cur_ni > prev_ni:
            score += 1; details.append("Earnings improving")

        # 8. Gross margin improving
        if cur_gp and cur_rev and prev_gp and prev_rev and cur_rev > 0 and prev_rev > 0:
            max_tests += 1
            if (cur_gp / cur_rev) > (prev_gp / prev_rev):
                score += 1; details.append("Margins improving")

        # 9. Asset turnover improving (approximate: revenue growth > 0)
        if cur_rev and prev_rev and prev_rev > 0:
            max_tests += 1
            if cur_rev > prev_rev:
                score += 1; details.append("Revenue growing YoY")

    # --- Tests approximated from current data ---

    # 5. Leverage: debt/equity reasonable or decreasing
    max_tests += 1
    if cur_de is not None and cur_de < 100:
        score += 1; details.append("Low leverage")

    # 6. Current ratio > 1 (liquidity adequate)
    max_tests += 1
    if cur_cr is not None and cur_cr > 1.0:
        score += 1; details.append("Adequate liquidity")

    # 7. No dilution (use sharesOutstanding from defaultKeyStatistics)
    cur_shares = _safe_raw(stats, "sharesOutstanding")
    float_shares = _safe_raw(stats, "floatShares")
    if cur_shares and float_shares:
        max_tests += 1
        # If float is close to outstanding, no significant dilution
        if float_shares / cur_shares > 0.85:
            score += 1; details.append("Minimal dilution")

    if max_tests < 5:
        return None, []  # insufficient data

    # Normalize to 0-9 scale if we couldn't run all 9 tests
    if max_tests < 9:
        score = round(score * 9 / max_tests)

    return min(score, 9), details



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

        divs = [s.dividend_yield for s in sector_stocks if s.dividend_yield is not None and s.dividend_yield > 0]
        des = [s.debt_to_equity for s in sector_stocks if s.debt_to_equity is not None and 0 < s.debt_to_equity < 500]
        pss = [s.price_to_sales for s in sector_stocks if s.price_to_sales is not None and s.price_to_sales > 0]

        averages[sector] = SectorAverages(
            sector=sector,
            avg_forward_pe=_avg(fpes),
            avg_price_to_book=_avg(pbs),
            avg_ev_to_ebitda=_avg(evs),
            avg_roe=_avg(roes),
            avg_dividend_yield=_avg(divs),
            avg_debt_to_equity=_avg(des),
            avg_price_to_sales=_avg(pss),
            stock_count=len(sector_stocks),
        )
    return averages


def _build_industry_groups(stocks: list[ScoredStock]) -> dict[str, list[ScoredStock]]:
    """Group stocks by industry."""
    by_industry: dict[str, list[ScoredStock]] = defaultdict(list)
    for s in stocks:
        if s.industry:
            by_industry[s.industry].append(s)
    return by_industry


def _industry_avg_excluding(
    peers: list[ScoredStock], exclude_symbol: str
) -> SectorAverages:
    """Compute industry average excluding a specific stock (leave-one-out)."""
    others = [s for s in peers if s.symbol != exclude_symbol]
    fpes = [
        s.forward_pe for s in others
        if s.forward_pe is not None and 0 < s.forward_pe < OUTLIER_FPE_MAX
    ]
    pbs = [
        s.price_to_book for s in others
        if s.price_to_book is not None and 0 < s.price_to_book < OUTLIER_PB_MAX
    ]
    evs = [
        s.ev_to_ebitda for s in others
        if s.ev_to_ebitda is not None and 0 < s.ev_to_ebitda < OUTLIER_EV_EBITDA_MAX
    ]
    roes = [
        s.return_on_equity for s in others
        if s.return_on_equity is not None
    ]
    divs = [s.dividend_yield for s in others if s.dividend_yield is not None and s.dividend_yield > 0]
    des = [s.debt_to_equity for s in others if s.debt_to_equity is not None and 0 < s.debt_to_equity < 500]
    pss = [s.price_to_sales for s in others if s.price_to_sales is not None and s.price_to_sales > 0]
    return SectorAverages(
        sector=peers[0].industry if peers else "",
        avg_forward_pe=_avg(fpes),
        avg_price_to_book=_avg(pbs),
        avg_ev_to_ebitda=_avg(evs),
        avg_roe=_avg(roes),
        avg_dividend_yield=_avg(divs),
        avg_debt_to_equity=_avg(des),
        avg_price_to_sales=_avg(pss),
        stock_count=len(others),
    )


async def compute_market_sector_averages(
    client: "YahooClient",
) -> dict[str, SectorAverages]:
    """Fetch blue-chip benchmarks per sector and compute market-level averages.

    This provides an unbiased reference — the 52W-low scan stocks skew cheap,
    so comparing against them understates how cheap a stock really is.
    """
    all_tickers: list[str] = []
    ticker_to_sector: dict[str, str] = {}
    for sector, tickers in SECTOR_BENCHMARK_TICKERS.items():
        for t in tickers:
            if t not in ticker_to_sector:  # GOOGL appears in two sectors
                all_tickers.append(t)
            ticker_to_sector[t] = sector

    logger.info(f"Fetching {len(all_tickers)} benchmark tickers for market averages...")
    raw = await client.fetch_fundamentals_batch(all_tickers)

    # Parse into fundamentals and group by sector
    by_sector: dict[str, list[StockFundamentals]] = defaultdict(list)
    for sym, data in raw.items():
        fund = parse_fundamentals(sym, data)
        sector = ticker_to_sector.get(sym, fund.sector)
        if sector:
            by_sector[sector].append(fund)

    averages: dict[str, SectorAverages] = {}
    for sector, funds in by_sector.items():
        fpes = [f.forward_pe for f in funds if f.forward_pe and 0 < f.forward_pe < OUTLIER_FPE_MAX]
        pbs = [f.price_to_book for f in funds if f.price_to_book and 0 < f.price_to_book < OUTLIER_PB_MAX]
        evs = [f.ev_to_ebitda for f in funds if f.ev_to_ebitda and 0 < f.ev_to_ebitda < OUTLIER_EV_EBITDA_MAX]
        roes = [f.return_on_equity for f in funds if f.return_on_equity is not None]
        divs = [f.dividend_yield for f in funds if f.dividend_yield is not None and f.dividend_yield > 0]
        des = [f.debt_to_equity for f in funds if f.debt_to_equity is not None and 0 < f.debt_to_equity < 500]
        pss = [f.price_to_sales for f in funds if f.price_to_sales is not None and f.price_to_sales > 0]
        averages[sector] = SectorAverages(
            sector=sector,
            avg_forward_pe=_avg(fpes),
            avg_price_to_book=_avg(pbs),
            avg_ev_to_ebitda=_avg(evs),
            avg_roe=_avg(roes),
            avg_dividend_yield=_avg(divs),
            avg_debt_to_equity=_avg(des),
            avg_price_to_sales=_avg(pss),
            stock_count=len(funds),
        )
        logger.info(
            f"  Market avg [{sector}]: P/E={averages[sector].avg_forward_pe}  "
            f"P/B={averages[sector].avg_price_to_book}  "
            f"EV/EBITDA={averages[sector].avg_ev_to_ebitda}  "
            f"({len(funds)} benchmarks)"
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
        s.country = fundamentals.country
        s.forward_pe = fundamentals.forward_pe
        s.price_to_book = fundamentals.price_to_book
        s.ev_to_ebitda = fundamentals.ev_to_ebitda
        s.return_on_equity = fundamentals.return_on_equity
        s.return_on_assets = fundamentals.return_on_assets
        s.free_cash_flow = fundamentals.free_cash_flow
        s.operating_cashflow = fundamentals.operating_cashflow
        s.enterprise_value = fundamentals.enterprise_value
        s.roic = fundamentals.roic
        s.recommendation_mean = fundamentals.recommendation_mean
        s.target_mean_price = fundamentals.target_mean_price
        s.price_to_sales = fundamentals.price_to_sales
        s.dividend_yield = fundamentals.dividend_yield
        s.short_percent_of_float = fundamentals.short_percent_of_float
        s.insider_buy_count = fundamentals.insider_buy_count
        s.insider_sell_count = fundamentals.insider_sell_count
        s.insider_net_shares = fundamentals.insider_net_shares
        s.piotroski_f_score = fundamentals.piotroski_f_score
        s.piotroski_details = fundamentals.piotroski_details
        s.gross_margin_change = fundamentals.gross_margin_change
        s.buyback_yield = fundamentals.buyback_yield
        s.debt_to_equity = fundamentals.debt_to_equity
        s.ev_to_revenue = fundamentals.ev_to_revenue
        s.revenue_growth = fundamentals.revenue_growth
        s.earnings_growth = fundamentals.earnings_growth
        s.current_ratio = fundamentals.current_ratio

    # Compute price momentum from 52W range
    if s.fifty_two_week_high and s.fifty_two_week_high > 0 and s.price:
        s.price_momentum_12m = round((s.price / s.fifty_two_week_high - 1) * 100, 1)
    # 3-month momentum approximated from change_percent
    if s.change_percent is not None:
        s.price_momentum_3m = round(s.change_percent, 1)

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

        # Compute relative momentum (stock drop vs sector avg drop)
        _compute_relative_momentum(stocks)

        # Step 5a: Compute scan-level sector and industry averages
        logger.info("Step 5a: Computing scan-level sector and industry averages...")
        sector_averages = compute_sector_averages(stocks)
        industry_groups = _build_industry_groups(stocks)
        ind_with_peers = sum(1 for g in industry_groups.values() if len(g) >= 4)
        logger.info(
            f"Industry groups: {len(industry_groups)} industries, "
            f"{ind_with_peers} with ≥3 peers (excl. self)"
        )

        # Step 5b: Fetch market-level sector benchmarks from blue chips
        logger.info("Step 5b: Fetching market benchmark averages...")
        market_averages = await compute_market_sector_averages(client)

        # Attach all averages to each stock
        for s in stocks:
            avg = sector_averages.get(s.sector)
            if avg:
                s.sector_avg_fpe = avg.avg_forward_pe
                s.sector_avg_pb = avg.avg_price_to_book
                s.sector_avg_ev_ebitda = avg.avg_ev_to_ebitda
                s.sector_avg_roe = avg.avg_roe
            mkt = market_averages.get(s.sector)
            if mkt:
                s.market_avg_fpe = mkt.avg_forward_pe
                s.market_avg_pb = mkt.avg_price_to_book
                s.market_avg_ev_ebitda = mkt.avg_ev_to_ebitda
                s.market_avg_roe = mkt.avg_roe
                s.market_avg_div_yield = mkt.avg_dividend_yield
                s.market_avg_debt_equity = mkt.avg_debt_to_equity
                s.market_avg_ps = mkt.avg_price_to_sales
            peers = industry_groups.get(s.industry, [])
            if len(peers) >= 2:
                ind_avg = _industry_avg_excluding(peers, s.symbol)
                s.industry_avg_fpe = ind_avg.avg_forward_pe
                s.industry_avg_pb = ind_avg.avg_price_to_book
                s.industry_avg_ev_ebitda = ind_avg.avg_ev_to_ebitda
                s.industry_avg_roe = ind_avg.avg_roe
                s.industry_peer_count = ind_avg.stock_count

        # Step 6: Score each stock
        # Priority: industry avg (≥3 peers) → market sector avg → scan sector avg
        logger.info("Step 6: Scoring stocks (value + quality)...")
        for s in stocks:
            peers = industry_groups.get(s.industry, [])
            if len(peers) >= 4:
                peer_avg = _industry_avg_excluding(peers, s.symbol)
            elif s.sector in market_averages:
                peer_avg = market_averages[s.sector]
            else:
                peer_avg = sector_averages.get(s.sector)
            # Value score (cheapness-focused)
            breakdown = compute_score(s, peer_avg)
            s.value_score = breakdown.total
            s.score_tier = breakdown.tier
            s.score_reasons = breakdown.reasons
            s.sector_type = breakdown.sector_type
            # Quality score (business quality at fair price)
            q = compute_quality_score(s, peer_avg)
            s.quality_score = q.total
            s.quality_tier = q.tier
            s.quality_reasons = q.reasons

        # Sort by score descending
        stocks.sort(key=lambda x: x.value_score, reverse=True)

        now = datetime.now(timezone.utc)
        result = ScanResult(
            scan_date=now.strftime("%Y-%m-%d"),
            scanned_at=now.isoformat(),
            total_stocks=len(stocks),
            stocks=stocks,
            sector_averages=sector_averages,
            market_sector_averages=market_averages,
        )

        logger.info(
            f"Pipeline complete: {len(stocks)} stocks scored. "
            f"Top score: {stocks[0].value_score if stocks else 0}"
        )
        return result

    finally:
        if own_client:
            await client.close()
