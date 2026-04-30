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
    create_portfolio_item, create_watchlist_item, delete_portfolio_item, delete_scan,
    delete_watchlist_item, get_backtest_details, get_backtest_summary,
    get_bounce_back_candidates, get_latest_good_scan, get_latest_scan,
    get_latest_scan_averages, get_mos_score, get_performance_rows_needing_update,
    get_portfolio_by_symbol, get_portfolio_item, get_recent_tracked_symbols,
    get_scan_by_date, get_scan_history, get_stock_history, get_watchlist_by_symbol,
    get_watchlist_item, init_db, list_portfolio, list_watchlist,
    save_performance_tracking, save_scan, update_forward_price, update_portfolio_item,
    update_watchlist_item, upsert_latest_price, upsert_mos_score,
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


# ──────────────── WATCHLIST SCHEMAS ────────────────

class WatchlistQuestion(BaseModel):
    """A single yes/no due-diligence question on a watchlist item."""
    question: str = Field(..., min_length=1, max_length=500)
    answer: str = Field(default="", max_length=2000)
    confirmed: bool = False

    model_config = {"extra": "forbid"}


class WatchlistCreate(BaseModel):
    """POST body for adding a stock to the watchlist."""
    symbol: str = Field(..., min_length=1, max_length=15)
    short_name: str = Field(default="", max_length=200)
    thesis: str = Field(default="", max_length=4000)
    target_price: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    target_event: str = Field(default="", max_length=200)
    # ISO YYYY-MM-DD; validated by validate_scan_date when provided.
    target_date: Optional[str] = Field(default=None, max_length=10)
    questions: list[WatchlistQuestion] = Field(default_factory=list, max_length=50)
    notes: str = Field(default="", max_length=10_000)
    # Snapshot is free-form (V/Q/F scores, price at add time, etc.) — bounded
    # by the parent body size so we don't need a per-key schema.
    snapshot: dict = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class WatchlistUpdate(BaseModel):
    """PATCH body — every field optional; only provided fields are written."""
    short_name: Optional[str] = Field(default=None, max_length=200)
    thesis: Optional[str] = Field(default=None, max_length=4000)
    target_price: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    target_event: Optional[str] = Field(default=None, max_length=200)
    target_date: Optional[str] = Field(default=None, max_length=10)
    questions: Optional[list[WatchlistQuestion]] = Field(default=None, max_length=50)
    status: Optional[str] = Field(default=None, pattern=r"^(watching|bought|rejected)$")
    notes: Optional[str] = Field(default=None, max_length=10_000)
    snapshot: Optional[dict] = None

    model_config = {"extra": "forbid"}


# ──────────────── PORTFOLIO SCHEMAS ────────────────
# A portfolio row is a current holding (or the synthetic CASH row). Cost
# basis is per-share avg; for CASH it's 1.0 and `shares` is the dollar
# amount. Bounds are generous — leave room for share-counts up to 10M and
# costs up to $1M/share — but reject negatives and absurd magnitudes.

class PortfolioCreate(BaseModel):
    """POST body for adding a position to the portfolio."""
    symbol: str = Field(..., min_length=1, max_length=15)
    short_name: str = Field(default="", max_length=200)
    shares: float = Field(..., ge=0, le=10_000_000)
    cost_basis: float = Field(..., ge=0, le=1_000_000)
    notes: str = Field(default="", max_length=4000)
    snapshot: dict = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class PortfolioUpdate(BaseModel):
    """PATCH body — every field optional; only provided fields are written."""
    short_name: Optional[str] = Field(default=None, max_length=200)
    shares: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    cost_basis: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    notes: Optional[str] = Field(default=None, max_length=4000)
    snapshot: Optional[dict] = None

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


async def recompute_mos_scores():
    """Refresh the deterministic Margin of Safety subscore for every symbol
    on the user's portfolio + watchlist. Runs once a day at 5:00 PM ET so
    the score reflects post-close fundamentals. Each score is computed from
    forward P/E + FCF yield + debt/equity via compute_mos_subscore() and
    persisted to symbol_latest_price.mos_score / mos_updated_at.

    The categorical pill on snapshot.margin_of_safety is not touched — that
    remains the human override. The UI surfaces both side-by-side so drift
    between manual rating and computed reality is visible.
    """
    global _yahoo_client
    if _yahoo_client is None:
        _yahoo_client = YahooClient()

    # Union of portfolio + watchlist symbols, dropping CASH (no fundamentals).
    pf_syms = {it["symbol"] for it in list_portfolio() if it["symbol"] != "CASH"}
    wl_syms = {it["symbol"] for it in list_watchlist()}
    symbols = sorted(pf_syms | wl_syms)
    logger.info(f"MoS recompute starting for {len(symbols)} symbols")

    async def _one(sym: str) -> tuple[str, Optional[dict]]:
        try:
            raw = await _yahoo_client.fetch_quote_summary(sym)
            if not raw:
                return sym, None
            quote = parse_quote_from_summary(sym, raw)
            fundamentals = parse_fundamentals(sym, raw)
            stock = merge_quote_and_fundamentals(quote, fundamentals)
            return sym, compute_mos_subscore(stock)
        except Exception as e:
            logger.debug(f"MoS recompute failed for {sym}: {e}")
            return sym, None

    results = await asyncio.gather(*(_one(s) for s in symbols))
    written = sum(1 for _, r in results if r is not None)
    for sym, r in results:
        if r is not None:
            try:
                upsert_mos_score(
                    sym,
                    r["score"],
                    peer_discount=r.get("peer_discount"),
                    dcf_discount=r.get("dcf_discount"),
                    asset_coverage=r.get("asset_coverage"),
                    intrinsic=r.get("intrinsic"),
                    dcf_bull=r.get("dcf_bull"),
                    dcf_base=r.get("dcf_base"),
                    dcf_bear=r.get("dcf_bear"),
                )
            except Exception as e:
                logger.warning(f"upsert_mos_score({sym}) failed: {e}")
    logger.info(f"MoS recompute done: {written}/{len(symbols)} scores written")


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
    scheduler.add_job(
        recompute_mos_scores,
        CronTrigger(
            hour=17,
            minute=0,
            day_of_week="mon-fri",
            timezone="US/Eastern",
        ),
        id="mos_recompute",
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


@app.get("/api/eps/{symbol}")
async def get_eps_history(symbol: str = Path(..., max_length=15)):
    """Return up to 5 years of annual diluted EPS for a symbol."""
    symbol = validate_symbol(symbol)
    global _yahoo_client
    if _yahoo_client is None:
        _yahoo_client = YahooClient()
    data = await _yahoo_client.fetch_eps_history(symbol)
    if not data:
        raise HTTPException(404, f"No EPS history for {symbol}")
    return data


@app.get("/api/fundamentals-history/{symbol}")
async def get_fundamentals_history(symbol: str = Path(..., max_length=15)):
    """Return bundled 5-10 years of annual fundamentals for the long-
    term-trends charts (Revenue, FCF vs NI, D/E, P/E bands, ROIC vs
    WACC, Dividend Yield). One call instead of six."""
    symbol = validate_symbol(symbol)
    global _yahoo_client
    if _yahoo_client is None:
        _yahoo_client = YahooClient()
    data = await _yahoo_client.fetch_fundamentals_history(symbol)
    if not data:
        raise HTTPException(404, f"No fundamentals history for {symbol}")
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


# ──────────────── WATCHLIST ENDPOINTS ────────────────
# Lightweight CRUD over the `watchlist` table. List endpoint also enriches
# each row with the latest known price (from the symbol_latest_price table
# the scan/fill jobs maintain) and computes a derived "ready_to_buy" flag
# the UI uses to highlight cards.

def _enrich_watchlist_item(item: dict, latest_prices: dict) -> dict:
    """Add current_price + ready_to_buy flag in place of pure DB fields."""
    sym = item["symbol"]
    info = latest_prices.get(sym) or {}
    cur = info.get("price") if isinstance(info, dict) else info
    item["current_price"] = cur
    target = item.get("target_price")
    questions = item.get("questions") or []
    all_confirmed = bool(questions) and all(q.get("confirmed") for q in questions)
    price_hit = (cur is not None and target is not None and cur <= target)
    # "Ready" = both signals fire, OR price hit with no questions defined.
    # When there are no questions, the price target alone is the trigger.
    item["price_hit"] = price_hit
    item["all_questions_confirmed"] = all_confirmed
    item["ready_to_buy"] = (
        item.get("status") == "watching"
        and price_hit
        and (all_confirmed or not questions)
    )
    return item


def _unwrap_yahoo(v):
    """Yahoo wraps numbers as {"raw": x, "fmt": "..."}; pull the float."""
    if isinstance(v, dict):
        v = v.get("raw")
    return float(v) if v is not None else None


def _peer_discount_pct(value: Optional[float], sector_median: Optional[float]) -> Optional[float]:
    """For a "lower is better" valuation ratio, return % discount vs sector
    median. Positive = trades cheaper than peers. None when either input
    is missing/non-positive. Cap at ±80% to avoid one outlier ratio
    swamping the average."""
    if value is None or value <= 0 or sector_median is None or sector_median <= 0:
        return None
    discount = (sector_median - value) / sector_median * 100
    return max(-80.0, min(80.0, discount))


def _dcf_value(fcf: float, shares: float, g: float, r: float, g_term: float) -> float:
    """Mechanical 5-year DCF + terminal value → per-share intrinsic.
    All inputs validated by the caller; this is just the arithmetic."""
    if r <= g_term:
        r = g_term + 0.04  # need r > g_term for terminal-value math
    pv_total = 0.0
    for yr in range(1, 6):
        fcf_yr = fcf * ((1 + g) ** yr)
        pv_total += fcf_yr / ((1 + r) ** yr)
    fcf_yr5 = fcf * ((1 + g) ** 5)
    terminal = fcf_yr5 * (1 + g_term) / (r - g_term)
    pv_total += terminal / ((1 + r) ** 5)
    return pv_total / shares


def _dcf_scenarios(stock) -> Optional[dict]:
    """Klarman-style scenario DCF — runs three independent valuations with
    different growth/discount/terminal assumptions, returns all three plus
    the weighted middle (25% bull + 50% base + 25% bear).

      Bear  — 0.5x base growth, +200 bps higher hurdle, 2.0% terminal
              ("things go wrong; assume premium for risk")
      Base  — clamped revenue/earnings growth, sector WACC (or 10% default),
              2.5% terminal (current methodology)
      Bull  — 1.5x base growth, -100 bps lower hurdle, 3.0% terminal
              ("things go right; modest optimism")

    Klarman's principle: triangulating intrinsic with multiple scenarios
    gives a band rather than a single point estimate. The dispersion itself
    is information — wide band = low confidence in the central estimate.

    Returns None when any required input is missing (FCF, shares, etc.).
    """
    fcf = getattr(stock, "free_cash_flow", None)
    shares = getattr(stock, "shares_outstanding", None)
    if not fcf or fcf <= 0 or not shares or shares <= 0:
        return None

    growth = getattr(stock, "revenue_growth", None)
    if growth is None:
        growth = getattr(stock, "earnings_growth", None)
    if growth is not None and abs(growth) > 1.5:
        growth = growth / 100  # normalize % → decimal
    g_base = max(0.0, min(0.12, growth or 0.03))

    wacc = getattr(stock, "sector_wacc", None)
    r_base = wacc if (wacc and 0.05 < wacc < 0.20) else 0.10

    # Bear: half the growth (floor at 0), 200 bps higher discount, 2% terminal
    g_bear = max(0.0, g_base * 0.5)
    r_bear = min(0.20, r_base + 0.02)
    bear = _dcf_value(fcf, shares, g_bear, r_bear, 0.020)

    # Base: as is
    base = _dcf_value(fcf, shares, g_base, r_base, 0.025)

    # Bull: 1.5x growth (cap at 15%), 100 bps lower discount, 3% terminal
    g_bull = min(0.15, g_base * 1.5)
    r_bull = max(0.06, r_base - 0.01)
    bull = _dcf_value(fcf, shares, g_bull, r_bull, 0.030)

    weighted = 0.25 * bull + 0.50 * base + 0.25 * bear

    return {
        "bull": round(bull, 2),
        "base": round(base, 2),
        "bear": round(bear, 2),
        "weighted": round(weighted, 2),
    }


def _asset_coverage_pct(stock, price: Optional[float]) -> Optional[float]:
    """Price as % of tangible book per share. < 100 means trades below TBV
    (Klarman's asset-floor MoS); 100 means at TBV; > 100 means above.
    Returns None when shares or book inputs aren't available."""
    if not price:
        return None
    shares = getattr(stock, "shares_outstanding", None)
    total_assets = getattr(stock, "total_assets", None)
    total_liabilities = getattr(stock, "total_liabilities", None)
    goodwill = getattr(stock, "goodwill", None) or 0
    if not shares or shares <= 0 or total_assets is None or total_liabilities is None:
        return None
    tangible_equity = total_assets - total_liabilities - goodwill
    if tangible_equity <= 0:
        return None
    tbv_per_share = tangible_equity / shares
    if tbv_per_share <= 0:
        return None
    return (price / tbv_per_share) * 100


def compute_mos_subscore(stock) -> Optional[dict]:
    """Klarman-style Margin of Safety = composite % discount to intrinsic
    value, computed three ways and weighted-averaged. Positive composite =
    stock trades below intrinsic; negative = at premium.

    Components (each is itself a discount %, then weighted):

      Peer-Relative Discount (33%)
        For each of [forward P/E, EV/EBITDA, P/B] vs sector median, compute
        (median - current) / median. Average the available discounts.
        Captures: "is it cheap relative to comparable companies?"

      DCF Discount (50%)
        Project FCF for 5 years at clamped growth (0-12%), terminal value
        at 2.5% perpetuity, discounted at sector WACC (or 10% fallback).
        Discount = (DCF_per_share - price) / DCF_per_share.
        Captures: "is it cheap relative to its own future cash flows?"

      Asset Coverage (17%)
        Price as % of tangible book per share. Trading below TBV → bonus
        discount equal to (100 - coverage). Above TBV → 0 contribution
        (we don't reward asset-light businesses, but we don't penalize
        them either; quality is for Quality scoring, not MoS).
        Captures: Klarman's asset-floor / liquidation safety.

    Returns dict with composite + the three component discounts + the
    estimated intrinsic value per share, or None when too few inputs are
    available to be meaningful.
    """
    price = getattr(stock, "price", None)

    # ── 1) Peer-relative discount ──────────────────────────────────────
    pe_disc = _peer_discount_pct(getattr(stock, "forward_pe", None),
                                  getattr(stock, "sector_median_pe", None))
    ev_disc = _peer_discount_pct(getattr(stock, "ev_to_ebitda", None),
                                  getattr(stock, "sector_median_ev_ebitda", None))
    pb_disc = _peer_discount_pct(getattr(stock, "price_to_book", None),
                                  getattr(stock, "sector_median_pb", None))
    peer_components = [d for d in (pe_disc, ev_disc, pb_disc) if d is not None]
    peer_discount = (sum(peer_components) / len(peer_components)) if peer_components else None

    # ── 2) DCF discount (scenario-weighted) ────────────────────────────
    # Klarman-style: bull/base/bear scenarios, weighted 25/50/25 to get
    # the central intrinsic estimate. Stored separately so the UI can show
    # the dispersion (wide band = low confidence in the point estimate).
    scenarios = _dcf_scenarios(stock)
    intrinsic = scenarios["weighted"] if scenarios else None
    if intrinsic and intrinsic > 0 and price and price > 0:
        dcf_discount = (intrinsic - price) / intrinsic * 100
        dcf_discount = max(-80.0, min(80.0, dcf_discount))
    else:
        dcf_discount = None

    # ── 3) Asset coverage ──────────────────────────────────────────────
    coverage_pct = _asset_coverage_pct(stock, price)
    # Convert coverage (price as % of TBV) to a "discount" contribution.
    # Below TBV → positive bonus; above TBV → 0 (we don't penalize since
    # tangible book is the floor, not the ceiling).
    if coverage_pct is not None:
        asset_discount = max(0.0, 100.0 - coverage_pct)
    else:
        asset_discount = None

    # ── Composite (weighted average) ───────────────────────────────────
    weights = {"peer": 0.33, "dcf": 0.50, "asset": 0.17}
    have = []
    weighted_sum = 0.0
    weight_total = 0.0
    if peer_discount is not None:
        weighted_sum += peer_discount * weights["peer"]
        weight_total += weights["peer"]
        have.append("peer")
    if dcf_discount is not None:
        weighted_sum += dcf_discount * weights["dcf"]
        weight_total += weights["dcf"]
        have.append("dcf")
    if asset_discount is not None:
        weighted_sum += asset_discount * weights["asset"]
        weight_total += weights["asset"]
        have.append("asset")

    # Need at least 2 of 3 components — single-axis MoS is too noisy.
    if len(have) < 2:
        return None
    composite = weighted_sum / weight_total

    return {
        "score": round(composite),
        "peer_discount": round(peer_discount) if peer_discount is not None else None,
        "dcf_discount": round(dcf_discount) if dcf_discount is not None else None,
        "asset_coverage": round(coverage_pct) if coverage_pct is not None else None,
        "intrinsic": round(intrinsic, 2) if intrinsic else None,
        "dcf_bull": scenarios["bull"] if scenarios else None,
        "dcf_base": scenarios["base"] if scenarios else None,
        "dcf_bear": scenarios["bear"] if scenarios else None,
    }


async def _fetch_latest_prices(symbols: list[str]) -> dict[str, dict]:
    """Read latest prices from symbol_latest_price (maintained by scan/fill).

    Returns a dict per symbol with {price, prev_close} so callers can compute
    day-change without a second roundtrip. For symbols not in cache — typically
    watchlist or portfolio items the scanner hasn't seen because they aren't
    at 52W lows — fall back to parallel `YahooClient.fetch_quote_summary` calls
    (the same path the daily scan and `/api/lookup` use; yfinance fast_info
    silently fails on Render's egress while the direct quoteSummary endpoint
    works fine). Successful lookups are persisted via upsert_latest_price so
    the next request reads from SQL instead of paying the network cost again.
    """
    from server.database import get_db
    out: dict[str, dict] = {}
    if not symbols:
        return out
    placeholders = ",".join("?" * len(symbols))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT symbol, price, prev_close FROM symbol_latest_price "
            f"WHERE symbol IN ({placeholders})",
            symbols,
        ).fetchall()
        for r in rows:
            out[r["symbol"]] = {"price": r["price"], "prev_close": r["prev_close"]}
    # Two reasons to refetch: missing entirely, or cached but no prev_close
    # yet (older rows written before the column existed).
    missing = [s for s in symbols if s not in out or out[s].get("prev_close") is None]
    if not missing:
        return out

    global _yahoo_client
    if _yahoo_client is None:
        _yahoo_client = YahooClient()

    async def _one(sym: str) -> tuple[str, Optional[float], Optional[float]]:
        try:
            data = await _yahoo_client.fetch_quote_summary(sym)
            if not data:
                return sym, None, None
            sd = data.get("summaryDetail", {}) or {}
            pr = data.get("price", {}) or {}
            price = _unwrap_yahoo(sd.get("regularMarketPrice") or pr.get("regularMarketPrice"))
            prev = _unwrap_yahoo(
                sd.get("regularMarketPreviousClose")
                or sd.get("previousClose")
                or pr.get("regularMarketPreviousClose")
            )
            return sym, price, prev
        except Exception as e:
            logger.debug(f"price fallback failed for {sym}: {e}")
            return sym, None, None

    results = await asyncio.gather(*(_one(s) for s in missing))
    for sym, price, prev in results:
        if price:
            out[sym] = {"price": price, "prev_close": prev}
            try:
                upsert_latest_price(sym, price, prev_close=prev)
            except Exception as e:
                logger.debug(f"upsert_latest_price({sym}) failed: {e}")
    return out


@app.get("/api/watchlist")
async def watchlist_list():
    """Return all watchlist items, enriched with current price + ready flag."""
    items = list_watchlist()
    prices = await _fetch_latest_prices([i["symbol"] for i in items])
    return {"items": [_attach_mos_score(_enrich_watchlist_item(i, prices)) for i in items]}


@app.post("/api/watchlist")
async def watchlist_create(body: WatchlistCreate):
    """Add a stock to the watchlist. 409 if already present."""
    symbol = validate_symbol(body.symbol)
    if body.target_date:
        validate_scan_date(body.target_date)
    if get_watchlist_by_symbol(symbol):
        raise HTTPException(409, f"{symbol} is already on the watchlist")
    try:
        item = create_watchlist_item(
            symbol=symbol,
            short_name=body.short_name,
            thesis=body.thesis,
            target_price=body.target_price,
            target_event=body.target_event,
            target_date=body.target_date,
            questions=[q.model_dump() for q in body.questions],
            notes=body.notes,
            snapshot=body.snapshot,
        )
    except Exception as e:
        logger.exception("watchlist create failed")
        raise HTTPException(500, f"Failed to create watchlist item: {e}")
    prices = await _fetch_latest_prices([symbol])
    return _enrich_watchlist_item(item, prices)


@app.patch("/api/watchlist/{item_id}")
async def watchlist_update(item_id: int, body: WatchlistUpdate):
    """Partial update — only fields present in the body are written."""
    if item_id <= 0:
        raise HTTPException(400, "item_id must be positive")
    if body.target_date:
        validate_scan_date(body.target_date)
    fields = body.model_dump(exclude_unset=True)
    if "questions" in fields and fields["questions"] is not None:
        fields["questions"] = [
            (q if isinstance(q, dict) else q.model_dump())
            for q in fields["questions"]
        ]
    item = update_watchlist_item(item_id, fields)
    if not item:
        raise HTTPException(404, f"Watchlist item {item_id} not found")
    prices = await _fetch_latest_prices([item["symbol"]])
    return _enrich_watchlist_item(item, prices)


@app.delete("/api/watchlist/{item_id}")
async def watchlist_delete(item_id: int):
    if item_id <= 0:
        raise HTTPException(400, "item_id must be positive")
    if not delete_watchlist_item(item_id):
        raise HTTPException(404, f"Watchlist item {item_id} not found")
    return {"deleted": item_id}


# ──────────────── PORTFOLIO ENDPOINTS ────────────────
# CRUD over the `portfolio` table. List endpoint enriches each row with
# current price + market value + P&L, and computes portfolio-level totals
# (NAV, cost basis, P&L %, weights). CASH is a synthetic row treated with
# price=1.0 so it flows through the same math without special cases above.

def _enrich_portfolio_item(item: dict, latest_prices: dict) -> dict:
    """Compute current_price, market_value, P&L, and day-change on a row."""
    sym = item["symbol"]
    if sym == "CASH":
        # Cash: shares are dollars, price is always $1, no P&L or day move.
        item["current_price"] = 1.0
        item["prev_close"] = 1.0
        item["market_value"] = float(item["shares"])
        item["total_cost"] = float(item["shares"])
        item["pnl"] = 0.0
        item["pnl_pct"] = 0.0
        item["day_change"] = 0.0
        item["day_change_pct"] = 0.0
        return item
    info = latest_prices.get(sym) or {}
    cur = info.get("price") if isinstance(info, dict) else info
    prev = info.get("prev_close") if isinstance(info, dict) else None
    item["current_price"] = cur
    item["prev_close"] = prev
    shares = float(item["shares"])
    cost = float(item["cost_basis"])
    item["total_cost"] = round(shares * cost, 2)
    if cur is None:
        # No quote available yet — surface the row but skip the math.
        item["market_value"] = None
        item["pnl"] = None
        item["pnl_pct"] = None
        item["day_change"] = None
        item["day_change_pct"] = None
    else:
        mv = shares * cur
        item["market_value"] = round(mv, 2)
        item["pnl"] = round(mv - shares * cost, 2)
        item["pnl_pct"] = round((cur / cost - 1) * 100, 2) if cost > 0 else None
        if prev and prev > 0:
            day_chg_per_share = cur - prev
            item["day_change"] = round(shares * day_chg_per_share, 2)
            item["day_change_pct"] = round((cur / prev - 1) * 100, 2)
        else:
            item["day_change"] = None
            item["day_change_pct"] = None
    return item


def _attach_mos_score(item: dict) -> dict:
    """Decorate a portfolio/watchlist item with the Klarman MoS composite +
    3 component discounts pulled from symbol_latest_price (recomputed by
    the daily scheduler). UI surfaces the headline % discount on the pill
    and the breakdown (peer / DCF / asset) plus implied intrinsic value
    in the hover tooltip."""
    sym = item.get("symbol")
    if sym and sym != "CASH":
        m = get_mos_score(sym)
        if m:
            item["mos_score"] = m.get("mos_score")
            item["mos_updated_at"] = m.get("mos_updated_at")
            item["mos_peer_discount"] = m.get("mos_peer_discount")
            item["mos_dcf_discount"] = m.get("mos_dcf_discount")
            item["mos_asset_coverage"] = m.get("mos_asset_coverage")
            item["mos_intrinsic"] = m.get("mos_intrinsic")
            item["mos_dcf_bull"] = m.get("mos_dcf_bull")
            item["mos_dcf_base"] = m.get("mos_dcf_base")
            item["mos_dcf_bear"] = m.get("mos_dcf_bear")
    return item


@app.post("/api/recompute-mos")
async def recompute_mos_endpoint():
    """Manual trigger for the MoS recompute job (otherwise runs daily at
    5 PM ET). Returns the count of symbols scored. Useful right after
    seeding new positions so the pills aren't blank until the next cron."""
    await recompute_mos_scores()
    return {"status": "ok"}


@app.get("/api/portfolio")
async def portfolio_list():
    """Return all portfolio rows + totals, enriched with current prices."""
    items = list_portfolio()
    # Pull live prices for everything except CASH, which is always $1.
    syms = [i["symbol"] for i in items if i["symbol"] != "CASH"]
    prices = await _fetch_latest_prices(syms)
    enriched = [_attach_mos_score(_enrich_portfolio_item(i, prices)) for i in items]

    # Portfolio-level totals. We tolerate missing market_value (price
    # lookup failed) by treating it as null — the UI shows the position
    # row but skips it from totals so weights still sum cleanly.
    nav = sum(i["market_value"] for i in enriched if i.get("market_value") is not None)
    total_cost = sum(i["total_cost"] for i in enriched if i.get("total_cost") is not None)
    pnl = nav - total_cost
    pnl_pct = (pnl / total_cost * 100) if total_cost > 0 else 0.0

    # Day-level totals: $ summed across positions where we have prev_close.
    # The % uses NAV-minus-day-change as the denominator so it represents
    # "how much did NAV move today" relative to yesterday's NAV.
    day_change_total = sum(
        i["day_change"] for i in enriched if i.get("day_change") is not None
    )
    nav_yday = nav - day_change_total
    day_change_pct = (day_change_total / nav_yday * 100) if nav_yday > 0 else 0.0

    # Position weights vs total NAV (cash included).
    for i in enriched:
        mv = i.get("market_value")
        i["weight_pct"] = round(mv / nav * 100, 2) if (mv is not None and nav > 0) else None

    return {
        "items": enriched,
        "totals": {
            "nav": round(nav, 2),
            "total_cost": round(total_cost, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "day_change": round(day_change_total, 2),
            "day_change_pct": round(day_change_pct, 2),
            "positions_count": sum(1 for i in enriched if i["symbol"] != "CASH"),
            "cash": next(
                (i["market_value"] for i in enriched if i["symbol"] == "CASH"), 0.0
            ),
        },
    }


@app.post("/api/portfolio")
async def portfolio_create(body: PortfolioCreate):
    """Add a position. 409 if symbol already present (use PATCH to edit)."""
    symbol = validate_symbol(body.symbol)
    if get_portfolio_by_symbol(symbol):
        raise HTTPException(409, f"{symbol} is already in the portfolio")
    try:
        item = create_portfolio_item(
            symbol=symbol,
            shares=body.shares,
            cost_basis=body.cost_basis,
            short_name=body.short_name,
            notes=body.notes,
            snapshot=body.snapshot,
        )
    except Exception as e:
        logger.exception("portfolio create failed")
        raise HTTPException(500, f"Failed to create portfolio item: {e}")
    prices = await _fetch_latest_prices([symbol]) if symbol != "CASH" else {}
    return _enrich_portfolio_item(item, prices)


@app.patch("/api/portfolio/{item_id}")
async def portfolio_update(item_id: int, body: PortfolioUpdate):
    """Partial update — change shares, cost basis, notes."""
    if item_id <= 0:
        raise HTTPException(400, "item_id must be positive")
    fields = body.model_dump(exclude_unset=True)
    # When the categorical MoS rating is being set/changed via the snapshot,
    # stamp a mos_reviewed_at timestamp so the UI can show "reviewed N days
    # ago" and flag stale ratings. Auto-recomputes (which only touch the
    # numeric mos_score, not the categorical) don't go through this path.
    if "snapshot" in fields and isinstance(fields["snapshot"], dict):
        snap = fields["snapshot"]
        if "margin_of_safety" in snap and not snap.get("mos_reviewed_at"):
            snap["mos_reviewed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    item = update_portfolio_item(item_id, fields)
    if not item:
        raise HTTPException(404, f"Portfolio item {item_id} not found")
    sym = item["symbol"]
    prices = await _fetch_latest_prices([sym]) if sym != "CASH" else {}
    return _attach_mos_score(_enrich_portfolio_item(item, prices))


@app.delete("/api/portfolio/{item_id}")
async def portfolio_delete(item_id: int):
    if item_id <= 0:
        raise HTTPException(400, "item_id must be positive")
    if not delete_portfolio_item(item_id):
        raise HTTPException(404, f"Portfolio item {item_id} not found")
    return {"deleted": item_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server.main:app", host=HOST, port=PORT, reload=True)
