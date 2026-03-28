"""FastAPI application — REST API + daily scheduler for the 52W Low Value Scanner."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.config import BASE_DIR, DAILY_REFRESH_HOUR, DAILY_REFRESH_MINUTE, HOST, PORT
from server.database import get_latest_scan, get_latest_scan_averages, get_scan_by_date, get_scan_history, get_stock_history, init_db, save_performance_tracking, save_scan
from server.models import ScanResult, ScanSummary
from server.pipeline import merge_quote_and_fundamentals, parse_fundamentals, parse_quote_from_summary, run_pipeline
from server.scorer import compute_quality_score, compute_score
from server.yahoo_client import YahooClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Global state
_refresh_lock = asyncio.Lock()
_yahoo_client: Optional[YahooClient] = None
_is_refreshing = False


async def scheduled_refresh():
    """Run by the scheduler at 4:30 PM ET daily."""
    logger.info("Scheduled daily refresh triggered")
    await _do_refresh()


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
    global _is_refreshing, _yahoo_client
    async with _refresh_lock:
        _is_refreshing = True
        try:
            if _yahoo_client is None:
                _yahoo_client = YahooClient()
            result = await run_pipeline(_yahoo_client)
            save_scan(result)
            save_performance_tracking(result.scan_date, result.stocks)
            logger.info(f"Scan saved: {result.scan_date} — {result.total_stocks} stocks")
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
        except Exception:
            logger.exception("Pipeline refresh failed")
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
    scheduler.start()
    logger.info(f"Scheduler started — daily refresh at {DAILY_REFRESH_HOUR}:{DAILY_REFRESH_MINUTE:02d} ET")
    yield
    scheduler.shutdown()
    if _yahoo_client:
        await _yahoo_client.close()


app = FastAPI(title="52W Low Value Scanner", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    """Return the latest scored stock list."""
    result = get_latest_scan()
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


@app.get("/api/scan/status")
async def scan_status():
    """Check if a refresh is in progress."""
    return {"refreshing": _is_refreshing}


@app.get("/api/scan/{scan_date}")
async def get_scan_for_date(scan_date: str):
    """Return scan for a specific date (YYYY-MM-DD)."""
    result = get_scan_by_date(scan_date)
    if not result:
        raise HTTPException(404, f"No scan found for {scan_date}")
    return _build_response(result)


@app.get("/api/spark/{symbol}")
async def get_spark(symbol: str):
    """Return 1-year price history for a symbol."""
    global _yahoo_client
    if _yahoo_client is None:
        _yahoo_client = YahooClient()
    data = await _yahoo_client.fetch_spark(symbol)
    if not data:
        raise HTTPException(404, f"No spark data for {symbol}")
    return data


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
async def update_settings(body: dict):
    """Update configurable settings at runtime."""
    from server import config
    if "china_adr_penalty" in body:
        val = int(body["china_adr_penalty"])
        config.CHINA_ADR_PENALTY = max(-25, min(0, val))
    if "use_damodaran_blend" in body:
        config.USE_DAMODARAN_BLEND = bool(body["use_damodaran_blend"])
    if "notify_enabled" in body:
        config.NOTIFY_ENABLED = bool(body["notify_enabled"])
    return await get_settings()


@app.get("/api/stock/{symbol}/history")
async def stock_history(symbol: str):
    """Return score history for a single stock across all scan dates."""
    history = get_stock_history(symbol)
    if not history:
        raise HTTPException(404, f"No history for {symbol}")
    return history


@app.get("/api/lookup/{symbol}")
async def lookup_stock(symbol: str):
    """Fetch, score, and return a single stock by symbol."""
    global _yahoo_client
    if _yahoo_client is None:
        _yahoo_client = YahooClient()

    symbol = symbol.upper().strip()

    # 1. Fetch quoteSummary
    raw = await _yahoo_client.fetch_quote_summary(symbol)
    if not raw:
        raise HTTPException(404, f"No data found for {symbol}")

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
