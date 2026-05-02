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
    """Fetch complete income statement + balance sheet + cash flow via yfinance.

    Also pulls quarterly financials (TTM fallback when annual prior-year is
    missing) and yfinance's insider_transactions endpoint (different source
    than Yahoo's quoteSummary insiderTransactions module — useful when that
    module is empty).
    """
    try:
        ticker = yf.Ticker(symbol)

        def _get(df, field: str, year: int = 0) -> Optional[float]:
            if df is None or df.empty or field not in df.index:
                return None
            val = df.iloc[:, year].get(field) if year < len(df.columns) else None
            if val is not None and not (isinstance(val, float) and val != val):
                return float(val)
            return None

        def _sum_q(df, field: str, cols: list) -> Optional[float]:
            """Sum a quarterly row across the given columns (TTM helper)."""
            if df is None or df.empty or field not in df.index:
                return None
            total = 0.0
            seen = 0
            for c in cols:
                v = df[c].get(field)
                if v is None or (isinstance(v, float) and v != v):
                    continue
                total += float(v)
                seen += 1
            # Require at least 3 of 4 quarters to consider TTM meaningful.
            return total if seen >= 3 else None

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

        # ── Cash flow statement (fallback for OCF/FCF when financialData lacks them) ──
        try:
            cf = ticker.cashflow
            result["cf_operating"] = (
                _get(cf, "Operating Cash Flow")
                or _get(cf, "Cash Flow From Continuing Operating Activities")
            )
            result["cf_free"] = _get(cf, "Free Cash Flow")
            result["cf_capex"] = _get(cf, "Capital Expenditure")
            # Multi-year FCF history for cyclical normalization. Most-recent
            # year first; up to 5 years. Skip None entries — gappy histories
            # still beat single-year TTM for cycle smoothing.
            cf_history: list[float] = []
            if cf is not None and not cf.empty:
                col_count = len(cf.columns)
                for yr in range(min(5, col_count)):
                    val = _get(cf, "Free Cash Flow", yr)
                    if val is None:
                        # Fall back to OCF - capex when FCF row is missing
                        ocf_y = _get(cf, "Operating Cash Flow", yr) or _get(
                            cf, "Cash Flow From Continuing Operating Activities", yr
                        )
                        capex_y = _get(cf, "Capital Expenditure", yr)
                        if ocf_y is not None and capex_y is not None:
                            val = ocf_y + capex_y  # capex is negative in Yahoo
                    if val is not None:
                        cf_history.append(float(val))
            if cf_history:
                result["cf_free_history"] = cf_history
        except Exception as e:
            logger.debug(f"cashflow fetch failed for {symbol}: {e}")

        # ── Quarterly fundamentals (TTM fallback when annual prior-year is missing) ──
        try:
            qfin = ticker.quarterly_financials
            if qfin is not None and not qfin.empty:
                cols = list(qfin.columns)
                if len(cols) >= 4:
                    ttm = cols[:4]
                    result["q_ttm_revenue"] = _sum_q(qfin, "Total Revenue", ttm)
                    result["q_ttm_net_income"] = _sum_q(qfin, "Net Income", ttm)
                    result["q_ttm_gross_profit"] = _sum_q(qfin, "Gross Profit", ttm)
                if len(cols) >= 8:
                    prev = cols[4:8]
                    result["q_prev_revenue"] = _sum_q(qfin, "Total Revenue", prev)
                    result["q_prev_net_income"] = _sum_q(qfin, "Net Income", prev)
                    result["q_prev_gross_profit"] = _sum_q(qfin, "Gross Profit", prev)
        except Exception as e:
            logger.debug(f"quarterly_financials fetch failed for {symbol}: {e}")

        # ── yfinance insider_transactions (different endpoint than Yahoo module) ──
        try:
            ins = ticker.insider_transactions
            if ins is not None and not ins.empty:
                # Normalize to a list of {transaction_text, shares, start_date_ts}
                import datetime
                rows = []
                for _, row in ins.iterrows():
                    sd = row.get("Start Date")
                    if isinstance(sd, datetime.datetime):
                        ts = int(sd.timestamp())
                    elif isinstance(sd, datetime.date):
                        ts = int(datetime.datetime(sd.year, sd.month, sd.day).timestamp())
                    else:
                        ts = 0
                    # `Text` is the descriptive field ("Purchase at price...",
                    # "Sale at price...", "Stock Award..."). `Transaction` is
                    # often empty in this endpoint.
                    # Capture `Position` and `Insider` so the classifier can
                    # filter out issuer (company) buybacks and compensation
                    # events. Without these, a company running an active NCIB
                    # gets dozens of "buybacks" mis-counted as insider buys
                    # (e.g. GIB showed 72 phantom buys; true count was 1).
                    rows.append({
                        "text": str(row.get("Text") or row.get("Transaction") or "").lower(),
                        "shares": int(row.get("Shares") or 0),
                        "ts": ts,
                        "position": str(row.get("Position") or "").strip().lower(),
                        "insider": str(row.get("Insider") or "").strip().lower(),
                    })
                result["insider_yf"] = rows
        except Exception as e:
            logger.debug(f"insider_transactions fetch failed for {symbol}: {e}")

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
                        # Also persist the date string so we can render
                        # "📅 May 6 (5d)" rather than just a day count.
                        result["next_earnings_date"] = next_earn.isoformat()
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


def _fetch_sec_insider(symbol: str) -> Optional[dict]:
    """SEC EDGAR Form-4 insider activity (last 180d) — fallback for foreign listings.

    Returns a dict with normalized buy/sell counts, or None if SEC has no data
    or the fetch failed. Cached per-day inside server.sec_edgar.
    """
    try:
        from server import sec_edgar
        result = sec_edgar.get_insider_activity(symbol, days=180)
        if result is None:
            return None
        buys, sells, net_shares = result
        return {"buys": buys, "sells": sells, "net_shares": net_shares}
    except Exception as e:
        logger.debug(f"SEC insider fetch failed for {symbol}: {e}")
        return None


def _looks_like_valid_crumb(c: Optional[str]) -> bool:
    """Real Yahoo crumbs are short opaque tokens (~11 chars, alphanumeric +
    a few punctuation). We've seen yfinance's bootstrap silently store
    error bodies like "Too Many Requests\\r\\n" as the crumb when Yahoo
    rate-limits the getcrumb endpoint — its substring check for
    "Too Many Requests" misses variants with extra whitespace or
    punctuation, and the error body becomes the crumb. Every subsequent
    quoteSummary call then 401s with "Invalid Crumb" forever.

    Treat anything that's empty, looks like an error message, contains
    HTML, or is suspiciously long as bogus.
    """
    if not c:
        return False
    s = c.strip()
    if not s or len(s) > 30:
        return False
    lower = s.lower()
    bad_substrings = ("too many", "request", "error", "<html", "html>", "unauthorized", "<!doctype")
    if any(b in lower for b in bad_substrings):
        return False
    # Real crumbs are URL-safe-ish — no whitespace, no angle brackets
    if any(ch.isspace() or ch in "<>" for ch in s):
        return False
    return True


def _get_session_and_crumb():
    """Get a yfinance session + crumb via YfData's singleton, with our own
    crumb sanity check on top.

    yfinance moved to a curl_cffi session in 1.x specifically to mimic a
    real browser's TLS fingerprint, which is what gets past Yahoo's
    datacenter-IP bot detection. The previous bootstrap (yf.Ticker.session
    + raw GET to /v1/test/getcrumb) used a plain requests.Session that has
    no browser fingerprint — fine for residential traffic, instantly
    blocked from Render/AWS/GCP IPs.

    YfData()._get_cookie_and_crumb() handles the full consent flow,
    sets the crumb on its singleton, and exposes the curl_cffi session
    via _session. BUT yfinance's basic-strategy crumb path can stash
    Yahoo's "Too Many Requests" error body AS the crumb when its
    substring guard misses (e.g. trailing CRLF). We layer our own
    `_looks_like_valid_crumb` check, force-reset the singleton's
    cached crumb/cookie when it looks bogus, and retry with a strategy
    flip + backoff.
    """
    import time as _time
    from yfinance.data import YfData
    from yfinance.exceptions import YFRateLimitError

    yfd = YfData()

    # If the singleton has a cached but invalid crumb (from a prior bad
    # bootstrap on this process), wipe it so we re-fetch.
    if yfd._crumb is not None and not _looks_like_valid_crumb(yfd._crumb):
        logger.warning(
            f"YfData has invalid cached crumb (len={len(yfd._crumb)}, "
            f"prefix={yfd._crumb[:12]!r}); clearing and re-bootstrapping"
        )
        yfd._crumb = None
        yfd._cookie = None

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            yfd._get_cookie_and_crumb()
        except YFRateLimitError as e:
            last_err = e
            # yfinance leaves _crumb populated with the error body — clear it
            yfd._crumb = None
            yfd._cookie = None
        except Exception as e:
            last_err = e
            yfd._crumb = None
            yfd._cookie = None

        if _looks_like_valid_crumb(yfd._crumb):
            logger.info(
                f"Got valid crumb after {attempt+1} attempt(s) "
                f"(strategy={yfd._cookie_strategy}, len={len(yfd._crumb)})"
            )
            return yfd._session, yfd._crumb

        # Bad crumb (or none) — flip strategy and back off
        bad_preview = (yfd._crumb or "")[:20]
        logger.warning(
            f"Crumb attempt {attempt+1} invalid "
            f"(strategy={yfd._cookie_strategy}, preview={bad_preview!r}, "
            f"err={last_err!r}); flipping strategy"
        )
        yfd._crumb = None
        yfd._cookie = None
        new_strategy = "basic" if yfd._cookie_strategy == "csrf" else "csrf"
        try:
            yfd._set_cookie_strategy(new_strategy)
        except Exception as e:
            logger.warning(f"Failed to flip cookie strategy: {e}")
        _time.sleep(2 ** attempt)  # 1s, 2s, 4s

    raise RuntimeError(
        f"YfData failed to obtain a valid crumb after 3 attempts "
        f"(last_err={last_err!r})"
    )


class YahooClient:
    """Yahoo Finance API client leveraging yfinance's auth session."""

    def __init__(self):
        self._session = None
        self._crumb: Optional[str] = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    def _ensure_session(self):
        """Initialize session if needed (sync, runs in executor).

        If crumb retrieval fails we leave both fields None so the next
        attempt re-runs the full bootstrap — previously a half-initialized
        client (session set, crumb None) would cache forever and every
        subsequent request would 401 with `crumb=None`.

        Also validates the cached crumb on every call, since yfinance can
        silently stash an error-message string as the crumb (see
        `_looks_like_valid_crumb` for context) and we don't want a single
        bad bootstrap to poison the entire scan.
        """
        if self._session is None or not _looks_like_valid_crumb(self._crumb):
            logger.info("Initializing yfinance session via YfData...")
            try:
                self._session, self._crumb = _get_session_and_crumb()
                logger.info(f"Session ready, crumb: {self._crumb[:8]}...")
            except Exception as e:
                logger.error(f"Session init failed: {e}")
                self._session = None
                self._crumb = None
                raise

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
                    qs = data.get("quoteSummary", {}) or {}
                    result = qs.get("result") or []
                    if not result:
                        # Yahoo's "soft block": HTTP 200 but the result is null,
                        # often with an auth-flavored error envelope like
                        # `{"description": "crumb not in cache", "code": "Bad Request"}`.
                        # Datacenter IPs (Render, AWS) hit this constantly while
                        # residential traffic sails through. Treat it like a 401
                        # — refresh the session crumb and retry — instead of
                        # returning None and silently dropping fundamentals for
                        # every symbol in the scan.
                        err = qs.get("error") or {}
                        err_desc = (err.get("description") or "").lower()
                        if attempt < 2:
                            logger.warning(
                                f"Empty quoteSummary for {symbol} "
                                f"(err={err_desc!r}), refreshing session "
                                f"(attempt {attempt+1})"
                            )
                            self._refresh_session()
                            params["crumb"] = self._crumb
                            _time.sleep(1)  # let the new session settle
                            continue
                        logger.warning(
                            f"No quoteSummary result for {symbol} after retries "
                            f"(last err: {err_desc!r})"
                        )
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
            # Enrich with yfinance financials (complete income statement +
            # cash flow + quarterly TTM + yfinance insider fallback).
            loop = asyncio.get_event_loop()
            fin_data = await loop.run_in_executor(_executor, _fetch_yf_financials, symbol)
            if fin_data:
                data["_yf_financials"] = fin_data

            # SEC EDGAR insider fallback — only if BOTH Yahoo paths are empty.
            # Foreign listings / ADRs often have empty insiderTransactions but
            # do file Form 4s with the SEC.
            yahoo_txns = (data.get("insiderTransactions") or {}).get("transactions", [])
            yf_txns = (fin_data or {}).get("insider_yf") or []
            if not yahoo_txns and not yf_txns:
                sec_data = await loop.run_in_executor(_executor, _fetch_sec_insider, symbol)
                if sec_data:
                    data["_sec_insider"] = sec_data

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

    async def fetch_eps_history(self, symbol: str) -> Optional[list[dict]]:
        """Fetch annual diluted EPS history (up to 5 most recent years).

        Returns a list of {year, eps} sorted ascending by year, or None
        if no history is available. Falls back to Basic EPS, then to
        Net Income / Diluted Shares if neither EPS row is present.
        """
        loop = asyncio.get_event_loop()

        def _fetch():
            try:
                ticker = yf.Ticker(symbol)
                stmt = ticker.income_stmt  # annual; columns are dates desc
                if stmt is None or stmt.empty:
                    return None
                row = None
                for key in ("Diluted EPS", "Basic EPS"):
                    if key in stmt.index:
                        row = stmt.loc[key]
                        break
                if row is None:
                    # Fallback: compute EPS from Net Income / Diluted Shares.
                    ni_key = next((k for k in ("Net Income", "Net Income Common Stockholders")
                                   if k in stmt.index), None)
                    sh_key = next((k for k in ("Diluted Average Shares", "Basic Average Shares")
                                   if k in stmt.index), None)
                    if not (ni_key and sh_key):
                        return None
                    ni_row = stmt.loc[ni_key]
                    sh_row = stmt.loc[sh_key]
                    out = []
                    for col in ni_row.index:
                        ni = ni_row.get(col)
                        sh = sh_row.get(col)
                        if ni is None or sh is None or sh == 0:
                            continue
                        try:
                            year = col.year if hasattr(col, "year") else int(str(col)[:4])
                            out.append({"year": year, "eps": round(float(ni) / float(sh), 2)})
                        except Exception:
                            continue
                    out.sort(key=lambda x: x["year"])
                    return out[-5:] if out else None
                out = []
                for col, val in row.items():
                    if val is None:
                        continue
                    try:
                        fval = float(val)
                    except (TypeError, ValueError):
                        continue
                    if fval != fval:  # NaN
                        continue
                    try:
                        year = col.year if hasattr(col, "year") else int(str(col)[:4])
                    except Exception:
                        continue
                    out.append({"year": year, "eps": round(fval, 2)})
                out.sort(key=lambda x: x["year"])
                return out[-5:] if out else None
            except Exception as e:
                logger.error(f"Error fetching EPS history for {symbol}: {e}")
                return None

        return await loop.run_in_executor(_executor, _fetch)

    async def fetch_fundamentals_history(self, symbol: str) -> Optional[dict]:
        """Bundle 5-10 years of annual fundamentals into one payload.

        Returns one row per fiscal year with revenue, net income, FCF,
        EBIT, tax provision, pretax income, total debt, total equity,
        diluted EPS, year-end price, and dividends paid that year. The
        client computes derived metrics (ROIC, P/E bands, div yield)
        from this bundle so we make one Yahoo round-trip instead of six.

        Designed to feed all the long-term-trend charts (Revenue, FCF
        vs NI, D/E, P/E banding, ROIC vs WACC, Dividend Yield).
        """
        loop = asyncio.get_event_loop()

        def _pick_row(stmt, *keys):
            """Return the first row in `stmt` matching any of `keys`."""
            if stmt is None or stmt.empty:
                return None
            for k in keys:
                if k in stmt.index:
                    return stmt.loc[k]
            return None

        def _to_float(v):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            if f != f:  # NaN
                return None
            return f

        def _year_of(col):
            try:
                return col.year if hasattr(col, "year") else int(str(col)[:4])
            except Exception:
                return None

        def _fetch():
            try:
                ticker = yf.Ticker(symbol)
                inc = ticker.income_stmt
                bal = ticker.balance_sheet
                cf  = ticker.cashflow
                if (inc is None or inc.empty) and (bal is None or bal.empty):
                    return None

                rev_row    = _pick_row(inc, "Total Revenue", "Operating Revenue")
                ni_row     = _pick_row(inc, "Net Income", "Net Income Common Stockholders")
                ebit_row   = _pick_row(inc, "EBIT", "Operating Income")
                tax_row    = _pick_row(inc, "Tax Provision", "Income Tax Expense")
                pretax_row = _pick_row(inc, "Pretax Income", "Income Before Tax")
                eps_row    = _pick_row(inc, "Diluted EPS", "Basic EPS")

                # Many companies (especially low-leverage ones like BMI)
                # don't have a "Total Debt" row in yfinance's balance sheet
                # — the line item only appears when there's debt to report.
                # Try a wider fallback list, including the components we'd
                # sum to reconstruct it.
                debt_row    = _pick_row(bal, "Total Debt", "Long Term Debt And Capital Lease Obligation",
                                        "Long Term Debt", "Net Debt")
                lt_debt_row = _pick_row(bal, "Long Term Debt")
                cur_debt_row = _pick_row(bal, "Current Debt", "Current Debt And Capital Lease Obligation",
                                         "Short Long Term Debt")
                eq_row     = _pick_row(bal, "Stockholders Equity", "Total Stockholder Equity",
                                       "Common Stock Equity", "Total Equity Gross Minority Interest")
                shares_row = _pick_row(bal, "Ordinary Shares Number", "Share Issued",
                                       "Common Stock Shares Outstanding")
                # Whether the balance sheet exists at all for this filing
                # — used to decide if a missing debt row should mean "zero"
                # (no debt issued) vs "unknown" (no filing).
                bal_has_data = bal is not None and not bal.empty

                fcf_row    = _pick_row(cf, "Free Cash Flow")
                ocf_row    = _pick_row(cf, "Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
                capex_row  = _pick_row(cf, "Capital Expenditure", "Capital Expenditures")

                # Collect all years that show up in any statement.
                years = set()
                for r in (rev_row, ni_row, ebit_row, tax_row, pretax_row, eps_row,
                          debt_row, lt_debt_row, cur_debt_row, eq_row, shares_row,
                          fcf_row, ocf_row, capex_row):
                    if r is not None:
                        for col in r.index:
                            y = _year_of(col)
                            if y:
                                years.add((y, col))
                if not years:
                    return None

                # Year-end prices: pull 11y of monthly closes once and pick
                # the last close of each calendar year.
                year_end_price = {}
                try:
                    hist = ticker.history(period="11y", interval="1mo", auto_adjust=False)
                    if hist is not None and not hist.empty:
                        for ts, row in hist.iterrows():
                            try:
                                year_end_price[ts.year] = float(row["Close"])
                            except Exception:
                                continue
                except Exception:
                    pass

                # Dividends per fiscal year (sum of all dividend payments
                # within the calendar year — close enough for a yield chart).
                div_per_year = {}
                try:
                    divs = ticker.dividends
                    if divs is not None and not divs.empty:
                        for ts, val in divs.items():
                            try:
                                y = ts.year
                                div_per_year[y] = div_per_year.get(y, 0.0) + float(val)
                            except Exception:
                                continue
                except Exception:
                    pass

                def _val(row, col):
                    if row is None:
                        return None
                    v = row.get(col)
                    return _to_float(v)

                annual = []
                # Sort by (year, col) — col is a Timestamp so this orders
                # multi-period years correctly (rare, but safe).
                for y, col in sorted(years, key=lambda x: (x[0], x[1])):
                    rev    = _val(rev_row, col)
                    ni     = _val(ni_row, col)
                    ebit   = _val(ebit_row, col)
                    tax    = _val(tax_row, col)
                    pretax = _val(pretax_row, col)
                    eps    = _val(eps_row, col)
                    debt   = _val(debt_row, col)
                    eq     = _val(eq_row, col)
                    shares = _val(shares_row, col)
                    # Reconstruct from components if the top-level row missed.
                    if debt is None:
                        lt = _val(lt_debt_row, col)
                        cur = _val(cur_debt_row, col)
                        if lt is not None or cur is not None:
                            debt = (lt or 0) + (cur or 0)
                    # Balance sheet exists for this year and equity is
                    # reported, but no debt row matched anywhere → company
                    # has no interest-bearing debt to disclose. Treat as 0.
                    if debt is None and eq is not None and bal_has_data:
                        debt = 0.0
                    fcf    = _val(fcf_row, col)
                    if fcf is None:
                        ocf   = _val(ocf_row, col)
                        capex = _val(capex_row, col)
                        if ocf is not None and capex is not None:
                            fcf = ocf + capex  # capex is negative in yfinance
                    annual.append({
                        "year":           y,
                        "revenue":        rev,
                        "net_income":     ni,
                        "fcf":            fcf,
                        "ebit":           ebit,
                        "tax_provision":  tax,
                        "pretax_income":  pretax,
                        "eps":            round(eps, 2) if eps is not None else None,
                        "total_debt":     debt,
                        "total_equity":   eq,
                        "shares_outstanding": shares,
                        "year_end_price": round(year_end_price[y], 2) if y in year_end_price else None,
                        "dividend":       round(div_per_year[y], 4) if y in div_per_year else None,
                    })

                # Keep the most recent 10 years.
                annual = annual[-10:]
                if not annual:
                    return None
                return {"annual": annual}
            except Exception as e:
                logger.error(f"Error fetching fundamentals history for {symbol}: {e}")
                return None

        return await loop.run_in_executor(_executor, _fetch)

    async def close(self):
        """Cleanup (no persistent connections to close with requests)."""
        pass
