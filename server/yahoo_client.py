"""Yahoo Finance API client using yfinance's session for auth bypass."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import yfinance as yf

from server.config import (
    MAX_CONCURRENT_REQUESTS,
    REQUEST_DELAY_MS,
    SCREENER_COUNT,
    YAHOO_QUOTE_SUMMARY_URL,
    YAHOO_SCREENER_URL,
    YAHOO_SPARK_URL,
)

logger = logging.getLogger(__name__)

# Thread pool for running sync yfinance calls
_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS)


def _fetch_yf_financials(symbol: str) -> Optional[dict]:
    """Fetch complete income statement + balance sheet via yfinance."""
    try:
        ticker = yf.Ticker(symbol)

        def _get(df, field: str, year: int = 0) -> Optional[float]:
            if df is None or df.empty or field not in df.index:
                return None
            val = df.iloc[:, year].get(field) if year < len(df.columns) else None
            if val is not None and not (isinstance(val, float) and val != val):
                return float(val)
            return None

        fin = ticker.financials
        bs = ticker.balance_sheet

        result = {
            # Income statement
            "ebit": _get(fin, "EBIT"),
            "ebitda": _get(fin, "EBITDA"),
            "ebitda_prev": _get(fin, "EBITDA", 1),
            "interest_expense": _get(fin, "Interest Expense"),
            "gross_profit": _get(fin, "Gross Profit"),
            "gross_profit_prev": _get(fin, "Gross Profit", 1),
            "total_revenue": _get(fin, "Total Revenue"),
            "total_revenue_prev": _get(fin, "Total Revenue", 1),
            "total_revenue_2yr": _get(fin, "Total Revenue", 2),
            "operating_income": _get(fin, "Operating Income"),
            "net_income": _get(fin, "Net Income"),
            "net_income_prev": _get(fin, "Net Income", 1),
            "depreciation": _get(fin, "Reconciled Depreciation") or _get(fin, "Depreciation And Amortization In Income Statement"),
            "sga": _get(fin, "Selling General And Administration"),
            "sga_prev": _get(fin, "Selling General And Administration", 1),
            # Balance sheet (current year)
            "bs_total_assets": _get(bs, "Total Assets"),
            "bs_total_assets_prev": _get(bs, "Total Assets", 1),
            "bs_current_assets": _get(bs, "Current Assets") or _get(bs, "Total Current Assets"),
            "bs_current_assets_prev": (_get(bs, "Current Assets", 1) or _get(bs, "Total Current Assets", 1)),
            "bs_total_liabilities": _get(bs, "Total Liabilities Net Minority Interest") or _get(bs, "Total Liab"),
            "bs_total_liabilities_prev": (_get(bs, "Total Liabilities Net Minority Interest", 1) or _get(bs, "Total Liab", 1)),
            "bs_current_liabilities": _get(bs, "Current Liabilities") or _get(bs, "Total Current Liabilities"),
            "bs_current_liabilities_prev": (_get(bs, "Current Liabilities", 1) or _get(bs, "Total Current Liabilities", 1)),
            "bs_ppe": _get(bs, "Net PPE") or _get(bs, "Gross PPE"),
            "bs_ppe_prev": (_get(bs, "Net PPE", 1) or _get(bs, "Gross PPE", 1)),
            "bs_receivables": _get(bs, "Receivables") or _get(bs, "Accounts Receivable"),
            "bs_receivables_prev": (_get(bs, "Receivables", 1) or _get(bs, "Accounts Receivable", 1)),
            "bs_long_term_debt": _get(bs, "Long Term Debt"),
            "bs_current_debt": _get(bs, "Current Debt") or _get(bs, "Current Portion Of Long Term Debt"),
            "bs_long_term_debt_prev": _get(bs, "Long Term Debt", 1),
            "bs_shares_outstanding": _get(bs, "Ordinary Shares Number") or _get(bs, "Share Issued"),
            "bs_cash": _get(bs, "Cash And Cash Equivalents"),
            "bs_short_term_investments": _get(bs, "Other Short Term Investments"),
            # Balance sheet extras for Priority 8
            "bs_goodwill": _get(bs, "Goodwill"),
            "bs_goodwill_prev": _get(bs, "Goodwill", 1),
            "bs_intangibles": _get(bs, "Other Intangible Assets"),
        }

        # Priority 5: Earnings date + Priority 6: shares short
        try:
            cal = ticker.calendar
            if cal and isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if ed and isinstance(ed, list) and len(ed) > 0:
                    import datetime
                    next_earn = ed[0]
                    if isinstance(next_earn, datetime.date):
                        days_to_earnings = (next_earn - datetime.date.today()).days
                        result["days_to_earnings"] = days_to_earnings
        except Exception:
            pass

        try:
            info = ticker.info
            result["shares_short"] = info.get("sharesShort")
            result["avg_daily_volume"] = info.get("averageDailyVolume10Day") or info.get("averageVolume")
        except Exception:
            pass

        return result
    except Exception as e:
        logger.error(f"Error fetching yf financials for {symbol}: {e}")
        return None


def _get_session():
    """Get a fresh yfinance session with valid cookies/crumb."""
    ticker = yf.Ticker("AAPL")
    return ticker.session


def _get_crumb(session) -> str:
    """Get crumb from the yfinance session."""
    resp = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb")
    resp.raise_for_status()
    return resp.text.strip()


class YahooClient:
    """Yahoo Finance API client leveraging yfinance's auth session."""

    def __init__(self):
        self._session = None
        self._crumb: Optional[str] = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    def _ensure_session(self):
        """Initialize session if needed (sync, runs in executor)."""
        if self._session is None:
            logger.info("Initializing yfinance session...")
            self._session = _get_session()
            self._crumb = _get_crumb(self._session)
            logger.info(f"Session ready, crumb: {self._crumb[:8]}...")

    def _refresh_session(self):
        """Force refresh the session."""
        self._session = None
        self._crumb = None
        self._ensure_session()

    def _sync_get(self, url: str, params: Optional[dict] = None, max_retries: int = 3) -> dict:
        """Sync GET with exponential backoff on 429/5xx and crumb refresh on 401."""
        import time as _time
        self._ensure_session()
        for attempt in range(max_retries + 1):
            resp = self._session.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                logger.warning(f"401 on attempt {attempt+1}, refreshing session...")
                self._refresh_session()
                if params and "crumb" in params:
                    params["crumb"] = self._crumb
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** (attempt + 1)  # 2, 4, 8 seconds
                logger.warning(f"HTTP {resp.status_code} on {url[:60]}, retry {attempt+1}/{max_retries} in {wait}s")
                _time.sleep(wait)
                continue
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()

    async def fetch_screener(self, offset: int = 0) -> list[dict]:
        """Step 1: Fetch 52-week low screener list."""
        loop = asyncio.get_event_loop()

        def _fetch():
            params = {
                "scrIds": "recent_52_week_lows",
                "count": SCREENER_COUNT,
                "offset": offset,
                "region": "US",
                "lang": "en-US",
            }
            data = self._sync_get(YAHOO_SCREENER_URL, params)
            try:
                quotes = data["finance"]["result"][0]["quotes"]
            except (KeyError, IndexError, TypeError):
                logger.error(f"Unexpected screener response: {str(data)[:500]}")
                return []
            logger.info(f"Fetched {len(quotes)} quotes from screener (offset={offset})")
            return quotes

        return await loop.run_in_executor(_executor, _fetch)

    async def fetch_quote_summary(self, symbol: str) -> Optional[dict]:
        """Step 4: Fetch fundamentals for a single symbol."""
        loop = asyncio.get_event_loop()

        def _fetch():
            import time as _time
            self._ensure_session()
            url = YAHOO_QUOTE_SUMMARY_URL.format(symbol=symbol)
            params = {
                "modules": "defaultKeyStatistics,financialData,summaryDetail,assetProfile,insiderTransactions,incomeStatementHistory,price,earningsHistory",
                "crumb": self._crumb,
            }
            for attempt in range(3):
                try:
                    resp = self._session.get(url, params=params)
                    if resp.status_code == 401:
                        logger.warning(f"401 for {symbol}, refreshing session (attempt {attempt+1})")
                        self._refresh_session()
                        params["crumb"] = self._crumb
                        continue
                    if resp.status_code == 429 or resp.status_code >= 500:
                        wait = 2 ** (attempt + 1)
                        logger.warning(f"HTTP {resp.status_code} for {symbol}, retry in {wait}s")
                        _time.sleep(wait)
                        continue
                    if resp.status_code != 200:
                        logger.warning(f"quoteSummary {symbol}: HTTP {resp.status_code}")
                        return None
                    data = resp.json()
                    result = data.get("quoteSummary", {}).get("result", [])
                    if not result:
                        logger.warning(f"No quoteSummary result for {symbol}")
                        return None
                    return result[0]
                except Exception as e:
                    logger.error(f"Error fetching {symbol} (attempt {attempt+1}): {e}")
                    if attempt < 2:
                        _time.sleep(2 ** (attempt + 1))
            logger.error(f"All retries exhausted for {symbol}")
            return None

        async with self._semaphore:
            result = await loop.run_in_executor(_executor, _fetch)
            await asyncio.sleep(REQUEST_DELAY_MS / 1000)
            return result

    async def fetch_fundamentals_batch(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch fundamentals for a batch of symbols with concurrency control."""
        results: dict[str, dict] = {}
        tasks = [self._fetch_one(sym, results) for sym in symbols]
        await asyncio.gather(*tasks)
        return results

    async def _fetch_one(self, symbol: str, results: dict):
        data = await self.fetch_quote_summary(symbol)
        if data:
            # Enrich with yfinance financials (complete income statement)
            loop = asyncio.get_event_loop()
            fin_data = await loop.run_in_executor(_executor, _fetch_yf_financials, symbol)
            if fin_data:
                data["_yf_financials"] = fin_data
            results[symbol] = data

    async def fetch_spark(self, symbol: str) -> Optional[list[dict]]:
        """Fetch 1-year price history for sparkline chart."""
        loop = asyncio.get_event_loop()

        def _fetch():
            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="1y", interval="1d")
                if hist.empty:
                    return None
                return [
                    {"t": int(ts.timestamp()), "c": round(row["Close"], 2)}
                    for ts, row in hist.iterrows()
                ]
            except Exception as e:
                logger.error(f"Error fetching spark for {symbol}: {e}")
                return None

        return await loop.run_in_executor(_executor, _fetch)

    async def close(self):
        """Cleanup (no persistent connections to close with requests)."""
        pass
