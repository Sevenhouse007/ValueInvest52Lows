"""FastAPI application — REST API + daily scheduler for the 52W Low Value Scanner."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from server import config
from server.config import BASE_DIR, DAILY_REFRESH_HOUR, DAILY_REFRESH_MINUTE, HOST, PORT
from server.database import (
    delete_scan, get_backtest_details, get_backtest_summary, get_bounce_back_candidates,
    get_latest_good_scan, get_latest_scan, get_latest_scan_averages,
    get_performance_rows_needing_update, get_recent_tracked_symbols, get_scan_by_date,
    get_scan_history, get_stock_history, init_db, save_performance_tracking, save_scan,
    update_forward_price, upsert_latest_price,
)
from server.models import ScanResult, ScanSummary
from server.pipeline import merge_quote_and_fundamentals, parse_fundamentals, parse_quote_from_summary, run_pipeline
from server.scorer import compute_quality_score, compute_score
from server.yahoo_client import YahooClient, _executor, _fetch_yf_financials

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ──────────────── INPUT VALIDATORS ────────────────
# Every endpoint that takes a path/query/body parameter from the client
# routes through one of these — never trust raw strings into SQL queries,
# Yahoo URLs, or HTML rendering paths.

# Tickers: uppercase letters/digits with `.` `-` `^` (indices) `=` (futures)
# allowed. Max 15 chars covers e.g. "BRK.B", "ESM=F", "^GSPC", and foreign
# suffixes like "ASML.AS" without admitting arbitrary content.
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^=]{1,15}$")
# Scan dates are always ISO YYYY-MM-DD as written by the pipeline.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_symbol(symbol: str) -> str:
    """Normalize + validate a ticker. Raises HTTPException(400) on bad input.

    Returns the normalized (upper, stripped) symbol so callers don't have
    to do it again. Reject anything that doesn't match the strict regex
    rather than silently passing through — a stray `;DROP TABLE` or
    `<script>` is never a valid ticker.
    """
    if not isinstance(symbol, str):
        raise HTTPException(400, "symbol must be a string")
    s = symbol.strip().upper()
    if not _SYMBOL_RE.match(s):
        raise HTTPException(400, f"Invalid ticker symbol: {symbol!r}")
    return s


def validate_scan_date(scan_date: str) -> str:
    """Validate a YYYY-MM-DD scan date. Rejects anything else."""
    if not isinstance(scan_date, str) or not _DATE_RE.match(scan_date):
        raise HTTPException(400, f"Invalid scan_date (expected YYYY-MM-DD): {scan_date!r}")
    try:
        # Confirms the date itself is real — rejects 2025-13-40 etc.
        datetime.strptime(scan_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"Invalid calendar date: {scan_date!r}")
    return scan_date


class SettingsUpdate(BaseModel):
    """Whitelist + bounds for `POST /api/settings`. Anything outside this
    schema is rejected before it touches `config.*` globals."""

    api_key: Optional[str] = Field(default=None, max_length=128)
    china_adr_penalty: Optional[int] = Field(default=None, ge=-25, le=0)
    use_damodaran_blend: Optional[bool] = None
    notify_enabled: Optional[bool] = None
    notify_top_n: Optional[int] = Field(default=None, ge=1, le=100)

    model_config = {"extra": "forbid"}


# Global state
_refresh_lock = asyncio.Lock()
_yahoo_client: Optional[YahooClient] = None
_is_refreshing = False
# Outcome of the most recent refresh attempt — surfaced via /api/scan/status
# so the UI can tell the user "data is from N hours ago, last refresh failed
# because Yahoo rate-limited" rather than silently overwriting good data
# with zeros.
_last_refresh_outcome: dict = {
    "status": "idle",          # idle | running | success | rejected | error
    "at": None,                # ISO timestamp of last completion
    "message": "",             # human-readable reason
    "fundamentals_coverage": None,  # fraction of stocks with non-zero V/sector
}


def _scan_quality(result: ScanResult) -> tuple[float, str]:
    """Return (coverage_fraction, summary_message) for a scan.

    Coverage = fraction of stocks with a non-empty sector AND a non-zero
    value_score. Used to decide whether a refresh result is worth saving
    or whether Yahoo soft-blocked the run and we should keep the previous
    good scan instead.
    """
    if not result.stocks:
        return 0.0, "0 stocks scored"
    good = sum(
        1 for s in result.stocks
        if (s.sector or "").strip() and (s.value_score or 0) > 0
    )
    total = len(result.stocks)
    frac = good / total
    return frac, f"{good}/{total} stocks have fundamentals"


# Refuse to overwrite the cached scan with a result where fewer than this
# fraction of stocks have real fundamentals (sector + V > 0). 30% is
# generous — a healthy run is ~95-100%; rate-limited runs are typically
# 0%. The middle ground (a partial outage) gets rejected too, on the
# theory that the cached scan is more useful than a half-broken one.
MIN_FUNDAMENTALS_COVERAGE = 0.30


async def scheduled_refresh():
    """Run by the scheduler at 4:30 PM ET daily."""
    logger.info("Scheduled daily refresh triggered")
    await _do_refresh()


async def fill_forward_returns():
    """Nightly job: fill in 15/30/90/180/365 day forward prices for performance
    tracking AND refresh latest known price for every symbol still in the
    365-day tracking window (used by bounce-back detection).
    """
    logger.info("Forward return fill job starting...")
    try:
        import yfinance as yf
        from datetime import datetime
        from collections import defaultdict

        rows = get_performance_rows_needing_update()
        # Group rows by symbol so we only fetch each symbol once
        rows_by_symbol: dict[str, list] = defaultdict(list)
        for r in rows:
            rows_by_symbol[r["symbol"]].append(r)

        # All symbols still tracked (whether or not they have rows needing update)
        recent_symbols = set(get_recent_tracked_symbols(365))
        all_symbols = recent_symbols | set(rows_by_symbol.keys())

        if not all_symbols:
            logger.info("No symbols to refresh")
            return

        logger.info(
            f"Refreshing {len(all_symbols)} symbols "
            f"({len(rows)} forward-return rows pending across {len(rows_by_symbol)} symbols)"
        )

        today = datetime.now()
        filled = 0
        latest_updated = 0
        for sym in all_symbols:
            try:
                ticker = yf.Ticker(sym)
                info = ticker.info
                price = info.get("regularMarketPrice") or info.get("currentPrice")
                if not price:
                    continue
                # Always upsert latest price for bounce-back detection
                upsert_latest_price(sym, float(price))
                latest_updated += 1
                # Then update any forward-return windows that need it
                for r in rows_by_symbol.get(sym, []):
                    scan_date = datetime.strptime(r["scan_date"], "%Y-%m-%d")
                    days_elapsed = (today - scan_date).days
                    if days_elapsed >= 15 and r["price_15d"] is None:
                        update_forward_price(r["id"], 15, price)
                        filled += 1
                    if days_elapsed >= 30 and r["price_30d"] is None:
                        update_forward_price(r["id"], 30, price)
                        filled += 1
                    if days_elapsed >= 90 and r["price_90d"] is None:
                        update_forward_price(r["id"], 90, price)
                        filled += 1
                    if days_elapsed >= 180 and r["price_180d"] is None:
                        update_forward_price(r["id"], 180, price)
                        filled += 1
                    if days_elapsed >= 365 and r["price_365d"] is None:
                        update_forward_price(r["id"], 365, price)
                        filled += 1
            except Exception as e:
                logger.warning(f"Forward return fill failed for {sym}: {e}")
        logger.info(
            f"Forward return fill complete: {filled} forward prices updated, "
            f"{latest_updated} latest prices refreshed"
        )
    except Exception as e:
        logger.error(f"Forward return fill job failed: {e}")


async def premarket_refresh():
    """Lightweight 7 AM ET job — update prices only, no fundamentals."""
    global _yahoo_client
    logger.info("Pre-market price refresh starting...")
    try:
        if _yahoo_client is None:
            _yahoo_client = YahooClient()
        latest = get_latest_scan()
        if not latest or not latest.stocks:
            logger.info("No scan data for pre-market refresh")
            return
        import yfinance as yf
        symbols = [s.symbol for s in latest.stocks[:50]]  # top 50 by score
        logger.info(f"Fetching pre-market prices for {len(symbols)} stocks...")
        tickers = yf.Tickers(" ".join(symbols))
        updated = 0
        for sym in symbols:
            try:
                info = tickers.tickers[sym].info
                price = info.get("regularMarketPrice") or info.get("currentPrice")
                low = info.get("fiftyTwoWeekLow")
                if price and low and price > low * 1.15:
                    logger.info(f"  {sym}: ${price:.2f} — 15%+ above 52W low, possible exit")
                updated += 1
            except Exception as e:
                logger.warning(f"Pre-market fetch failed for {sym}: {e}")
        logger.info(f"Pre-market refresh complete: {updated} stocks checked")
    except Exception as e:
        logger.error(f"Pre-market refresh failed: {e}")


async def _do_refresh() -> Optional[ScanResult]:
    global _is_refreshing, _yahoo_client, _last_refresh_outcome
    async with _refresh_lock:
        _is_refreshing = True
        _last_refresh_outcome = {
            "status": "running",
            "at": None,
            "message": "Refresh in progress…",
            "fundamentals_coverage": None,
        }
        try:
            if _yahoo_client is None:
                _yahoo_client = YahooClient()
            result = await run_pipeline(_yahoo_client)

            # Quality gate: don't overwrite the cached good scan with a
            # half-broken one. If Yahoo soft-blocks the run (rate limit,
            # invalid crumb cascade, etc.), most stocks will have V=0
            # and empty sectors — saving that wipes out yesterday's
            # perfectly good data and the UI looks dead.
            coverage, summary = _scan_quality(result)
            now_iso = datetime.now(timezone.utc).isoformat()
            if coverage < MIN_FUNDAMENTALS_COVERAGE:
                msg = (
                    f"Refresh rejected: {summary} (coverage {coverage:.0%} "
                    f"< {MIN_FUNDAMENTALS_COVERAGE:.0%} threshold). "
                    f"Likely Yahoo rate-limit. Cached scan kept."
                )
                logger.warning(msg)
                _last_refresh_outcome = {
                    "status": "rejected",
                    "at": now_iso,
                    "message": msg,
                    "fundamentals_coverage": round(coverage, 3),
                }
                return None

            save_scan(result)
            save_performance_tracking(result.scan_date, result.stocks)
            logger.info(
                f"Scan saved: {result.scan_date} — {result.total_stocks} "
                f"stocks ({summary}, coverage {coverage:.0%})"
            )
            _last_refresh_outcome = {
                "status": "success",
                "at": now_iso,
                "message": f"Saved {result.total_stocks} stocks ({summary})",
                "fundamentals_coverage": round(coverage, 3),
            }
            # Send notifications
            try:
                from server.notifications import send_daily_digest
                prev_scan = get_scan_by_date(
                    (datetime.now(timezone.utc) - __import__('datetime').timedelta(days=1)).strftime("%Y-%m-%d")
                )
                prev_symbols = {s.symbol for s in prev_scan.stocks} if prev_scan else set()
                send_daily_digest(result.scan_date, result.stocks, prev_symbols)
            except Exception as e:
                logger.warning(f"Notification failed: {e}")
            return result
        except Exception as e:
            logger.exception("Pipeline refresh failed")
            _last_refresh_outcome = {
                "status": "error",
                "at": datetime.now(timezone.utc).isoformat(),
                "message": f"{type(e).__name__}: {e}",
                "fundamentals_coverage": None,
            }
            return None
        finally:
            _is_refreshing = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_refresh,
        CronTrigger(
            hour=DAILY_REFRESH_HOUR,
            minute=DAILY_REFRESH_MINUTE,
            timezone="US/Eastern",
        ),
        id="daily_scan",
        replace_existing=True,
    )
    scheduler.add_job(
        premarket_refresh,
        CronTrigger(
            hour=7,
            minute=0,
            day_of_week="mon-fri",
            timezone="US/Eastern",
        ),
        id="premarket_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        fill_forward_returns,
        CronTrigger(
            hour=0,
            minute=30,
            timezone="US/Eastern",
        ),
        id="forward_returns",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started — daily refresh at {DAILY_REFRESH_HOUR}:{DAILY_REFRESH_MINUTE:02d} ET")
    yield
    scheduler.shutdown()
    if _yahoo_client:
        await _yahoo_client.close()


app = FastAPI(title="52W Low Value Scanner", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend
CLIENT_DIR = BASE_DIR / "client"
app.mount("/static", StaticFiles(directory=str(CLIENT_DIR)), name="static")


# ──────────────── API ROUTES ────────────────


@app.get("/")
async def index():
    return FileResponse(str(CLIENT_DIR / "index.html"))


@app.get("/api/scan")
async def get_scan():
    """Return the latest user-facing scan.

    Uses `get_latest_good_scan()` so a corrupt latest scan (Yahoo soft-
    blocked mid-fetch) doesn't blank the page — we walk back through
    history and return the most recent scan with real fundamentals
    coverage. If no scan in history clears the bar, we still return
    the most recent (degraded) one rather than 404, so the UI always
    has something to render. The refresh-status banner explains the
    degradation when it happens.
    """
    result = get_latest_good_scan()
    if not result:
        raise HTTPException(404, "No scan data available. Trigger a refresh first.")
    return _build_response(result)


@app.get("/api/scan/history")
async def scan_history():
    """Return available scan dates."""
    return get_scan_history()


@app.post("/api/scan/refresh")
async def trigger_refresh(background_tasks: BackgroundTasks):
    """Trigger a manual full refresh."""
    if _is_refreshing:
        return {"status": "already_running", "message": "A refresh is already in progress."}
    background_tasks.add_task(_do_refresh)
    return {"status": "started", "message": "Refresh started in background."}


@app.delete("/api/scan/{scan_date}")
async def delete_scan_endpoint(scan_date: str = Path(..., max_length=10)):
    """Delete a corrupt cached scan by date.

    Used to clean up scans saved before the quality-gate guard was added
    (where Yahoo soft-blocked mid-run and stocks ended up with V=0).
    After deletion the next refresh, or `get_latest_scan()` falling back
    to the previous date, surfaces a real result.
    """
    scan_date = validate_scan_date(scan_date)
    deleted = delete_scan(scan_date)
    if deleted == 0:
        raise HTTPException(404, f"No scan found for {scan_date}")
    return {"status": "deleted", "scan_date": scan_date, "rows_removed": deleted}


@app.get("/api/scan/status")
async def scan_status():
    """Check if a refresh is in progress and report the last outcome.

    The `last_refresh` block lets the UI distinguish "no scan yet" from
    "yesterday's scan is current because today's refresh failed". When
    `last_refresh.status == "rejected"` the cached scan is intentionally
    older than the displayed `at` timestamp.
    """
    return {
        "refreshing": _is_refreshing,
        "last_refresh": _last_refresh_outcome,
    }


@app.get("/api/scan/{scan_date}")
async def get_scan_for_date(scan_date: str = Path(..., max_length=10)):
    """Return scan for a specific date (YYYY-MM-DD)."""
    scan_date = validate_scan_date(scan_date)
    result = get_scan_by_date(scan_date)
    if not result:
        raise HTTPException(404, f"No scan found for {scan_date}")
    return _build_response(result)


@app.get("/api/spark/{symbol}")
async def get_spark(symbol: str = Path(..., max_length=15)):
    """Return 1-year price history for a symbol."""
    symbol = validate_symbol(symbol)
    global _yahoo_client
    if _yahoo_client is None:
        _yahoo_client = YahooClient()
    data = await _yahoo_client.fetch_spark(symbol)
    if not data:
        raise HTTPException(404, f"No spark data for {symbol}")
    return data


@app.get("/api/backtest/summary")
async def backtest_summary():
    """Return backtest summary: average returns by score tier."""
    return get_backtest_summary()


@app.get("/api/backtest/details")
async def backtest_details():
    """Return all performance tracking rows with forward returns."""
    return get_backtest_details()


@app.post("/api/backtest/fill")
async def trigger_forward_fill(background_tasks: BackgroundTasks):
    """Manually trigger the forward return fill job."""
    background_tasks.add_task(fill_forward_returns)
    return {"status": "started", "message": "Forward return fill started in background."}


@app.get("/api/bounce-back")
async def bounce_back(
    threshold: float = Query(0.05, ge=0.0, le=1.0),
    lookback_days: int = Query(90, ge=1, le=365),
):
    """Return stocks that hit a 52W low within `lookback_days` and have
    since rebounded by at least `threshold` (default 5%, 90-day window).

    Each entry includes the captured low (deepest dip we caught), the
    current price (refreshed daily by the fill job), the gain since the
    low, and the original V/Q scores at the time we picked it.

    Bounds enforced by FastAPI's Query validator before the handler runs;
    the redundant Python-side clamps are kept as defense-in-depth in case
    a future caller bypasses the validator.
    """
    threshold = max(0.0, min(1.0, threshold))
    lookback_days = max(1, min(365, lookback_days))
    candidates = get_bounce_back_candidates(threshold_pct=threshold, lookback_days=lookback_days)
    return {
        "threshold_pct": threshold,
        "lookback_days": lookback_days,
        "count": len(candidates),
        "bouncers": candidates,
    }


@app.get("/api/settings")
async def get_settings():
    """Return current configurable settings."""
    from server import config
    return {
        "china_adr_penalty": config.CHINA_ADR_PENALTY,
        "use_damodaran_blend": config.USE_DAMODARAN_BLEND,
        "notify_enabled": config.NOTIFY_ENABLED,
        "notify_top_n": config.NOTIFY_TOP_N,
    }


@app.post("/api/settings")
async def update_settings(body: SettingsUpdate):
    """Update configurable settings at runtime. Protected by API key if set.

    Body is validated against the SettingsUpdate Pydantic schema —
    unknown keys are rejected (`extra="forbid"`), types are checked, and
    bounds are enforced (e.g. china_adr_penalty must be -25..0). Only
    fields explicitly set in the request body are applied; others stay
    untouched.
    """
    from server import config
    # Auth check: if SETTINGS_API_KEY is configured, require it
    if config.SETTINGS_API_KEY:
        if (body.api_key or "") != config.SETTINGS_API_KEY:
            raise HTTPException(403, "Invalid API key")
    if body.china_adr_penalty is not None:
        config.CHINA_ADR_PENALTY = body.china_adr_penalty
    if body.use_damodaran_blend is not None:
        config.USE_DAMODARAN_BLEND = body.use_damodaran_blend
    if body.notify_enabled is not None:
        config.NOTIFY_ENABLED = body.notify_enabled
    if body.notify_top_n is not None:
        config.NOTIFY_TOP_N = body.notify_top_n
    return await get_settings()


@app.get("/api/stock/{symbol}/history")
async def stock_history(symbol: str = Path(..., max_length=15)):
    """Return score history for a single stock across all scan dates."""
    symbol = validate_symbol(symbol)
    history = get_stock_history(symbol)
    if not history:
        raise HTTPException(404, f"No history for {symbol}")
    return history


@app.get("/api/lookup/{symbol}")
async def lookup_stock(symbol: str = Path(..., max_length=15)):
    """Fetch, score, and return a single stock by symbol."""
    symbol = validate_symbol(symbol)
    global _yahoo_client
    if _yahoo_client is None:
        _yahoo_client = YahooClient()

    # 1. Fetch quoteSummary
    raw = await _yahoo_client.fetch_quote_summary(symbol)

    # Yahoo uses "BRK-B" for class shares but the standard ticker is "BRK.B".
    # If the dotted form returned only summaryDetail (no fundamentals), retry
    # with hyphens. Skip for foreign exchange suffixes like ".L"/".PA"/".T"
    # which legitimately use a dot.
    _EXCHANGE_SUFFIXES = (".L", ".PA", ".T", ".HK", ".TO", ".AX", ".DE", ".SW", ".AS", ".MI", ".MX")
    if (
        "." in symbol
        and not symbol.endswith(_EXCHANGE_SUFFIXES)
        and (not raw or "assetProfile" not in raw)
    ):
        retry = symbol.replace(".", "-")
        retry_raw = await _yahoo_client.fetch_quote_summary(retry)
        if retry_raw and "assetProfile" in retry_raw:
            symbol = retry
            raw = retry_raw
    if not raw:
        raise HTTPException(404, f"No data found for {symbol}")

    # 1b. Enrich with yfinance financials so balance-sheet driven metrics
    # (NOPAT-based ROIC, accruals, F-Score, asset growth, etc.) match what
    # the scanner produces. Without this, lookup falls back to OCF/EV ROIC
    # while the scan shows NOPAT ROIC — a confusing inconsistency.
    loop = asyncio.get_event_loop()
    fin_data = await loop.run_in_executor(_executor, _fetch_yf_financials, symbol)
    if fin_data:
        raw["_yf_financials"] = fin_data

    # 2. Parse quote + fundamentals
    quote = parse_quote_from_summary(symbol, raw)
    fundamentals = parse_fundamentals(symbol, raw)
    stock = merge_quote_and_fundamentals(quote, fundamentals)

    # 3. Get cached averages from latest scan
    sector_averages, market_averages = get_latest_scan_averages()

    # 4. Attach sector/market averages
    avg = sector_averages.get(stock.sector)
    if avg:
        stock.sector_avg_fpe = avg.avg_forward_pe
        stock.sector_avg_pb = avg.avg_price_to_book
        stock.sector_avg_ev_ebitda = avg.avg_ev_to_ebitda
        stock.sector_avg_roe = avg.avg_roe
    mkt = market_averages.get(stock.sector)
    if mkt:
        stock.market_avg_fpe = mkt.avg_forward_pe
        stock.market_avg_pb = mkt.avg_price_to_book
        stock.market_avg_ev_ebitda = mkt.avg_ev_to_ebitda
        stock.market_avg_roe = mkt.avg_roe
        stock.market_avg_div_yield = mkt.avg_dividend_yield
        stock.market_avg_debt_equity = mkt.avg_debt_to_equity
        stock.market_avg_ps = mkt.avg_price_to_sales

    # 5. Score (use market avg for peer comparison)
    peer_avg = mkt if mkt else avg
    breakdown = compute_score(stock, peer_avg)
    stock.value_score = breakdown.total
    stock.score_tier = breakdown.tier
    stock.score_reasons = breakdown.reasons
    stock.sector_type = breakdown.sector_type

    q = compute_quality_score(stock, peer_avg)
    stock.quality_score = q.total
    stock.quality_tier = q.tier
    stock.quality_reasons = q.reasons

    return {"stock": stock.model_dump()}


def _build_response(result: ScanResult) -> dict:
    """Build API response with summary cards."""
    stocks = result.stocks
    strong = [s for s in stocks if s.value_score >= 70]
    avg_score = round(sum(s.value_score for s in stocks) / len(stocks), 1) if stocks else 0

    # Find sector with most strong-value stocks
    from collections import Counter
    sector_counts = Counter(s.sector for s in strong if s.sector)
    top_sector, top_count = sector_counts.most_common(1)[0] if sector_counts else ("", 0)

    quality_buys = [s for s in stocks if s.quality_score >= 65]

    summary = ScanSummary(
        total_scanned=len(stocks),
        strong_value_count=len(strong),
        average_score=avg_score,
        top_sector=top_sector,
        top_sector_count=top_count,
    )
    # Compute rolling averages and days_in_scan (single batch query)
    from server.database import get_rolling_scores_batch
    rolling = get_rolling_scores_batch([s.symbol for s in stocks])
    stock_dicts = []
    for s in stocks:
        d = s.model_dump()
        r = rolling.get(s.symbol)
        if r:
            d["rolling_value_score"] = r["rolling_value"]
            d["rolling_quality_score"] = r["rolling_quality"]
            d["days_in_scan"] = r["days"]
        else:
            d["rolling_value_score"] = s.value_score
            d["rolling_quality_score"] = s.quality_score
            d["days_in_scan"] = 1
        stock_dicts.append(d)

    return {
        "scan_date": result.scan_date,
        "scanned_at": result.scanned_at,
        "summary": summary.model_dump(),
        "quality_buy_count": len(quality_buys),
        "stocks": stock_dicts,
        "sector_averages": {k: v.model_dump() for k, v in result.sector_averages.items()},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server.main:app", host=HOST, port=PORT, reload=True)
