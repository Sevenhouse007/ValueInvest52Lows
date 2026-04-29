"""SQLite database operations for persisting scan results."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from server.config import DB_PATH
from server.models import ScanHistoryEntry, ScanResult, ScoredStock, SectorAverages


def _ensure_db_dir():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_db():
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_date TEXT NOT NULL,
                scanned_at TEXT NOT NULL,
                total_stocks INTEGER DEFAULT 0,
                sector_averages_json TEXT DEFAULT '{}',
                UNIQUE(scan_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_stocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                data_json TEXT NOT NULL,
                value_score INTEGER DEFAULT 0,
                sector TEXT DEFAULT '',
                UNIQUE(scan_date, symbol)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_stocks_date
            ON scan_stocks(scan_date)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_stocks_score
            ON scan_stocks(value_score DESC)
        """)
        # Performance tracking table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                scan_date TEXT NOT NULL,
                price_at_scan REAL,
                price_15d REAL,
                price_30d REAL,
                price_90d REAL,
                price_180d REAL,
                price_365d REAL,
                return_15d REAL,
                return_30d REAL,
                return_90d REAL,
                return_180d REAL,
                return_365d REAL,
                value_score INTEGER,
                quality_score INTEGER,
                UNIQUE(scan_date, symbol)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_perf_symbol ON scan_performance(symbol)")

        # Latest known price per symbol — used by bounce-back detection.
        # Updated by both the daily scan (for symbols still at 52W low) and
        # the nightly fill job (for symbols still in our 365-day window).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS symbol_latest_price (
                symbol TEXT PRIMARY KEY,
                price REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Migration: add market_sector_averages_json if not present
        try:
            conn.execute("SELECT market_sector_averages_json FROM scans LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE scans ADD COLUMN market_sector_averages_json TEXT DEFAULT '{}'")

        # Migration: add 15-day forward-return columns to scan_performance.
        # Older deploys created the table before this window existed.
        try:
            conn.execute("SELECT price_15d FROM scan_performance LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE scan_performance ADD COLUMN price_15d REAL")
            conn.execute("ALTER TABLE scan_performance ADD COLUMN return_15d REAL")

        # Migration: add 365-day forward-return columns to scan_performance.
        # 1-year window captures full value-thesis horizons.
        try:
            conn.execute("SELECT price_365d FROM scan_performance LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE scan_performance ADD COLUMN price_365d REAL")
            conn.execute("ALTER TABLE scan_performance ADD COLUMN return_365d REAL")

        # Migration: enforce UNIQUE(scan_date, symbol) on scan_performance.
        # The constraint is in the CREATE TABLE for new deploys, but older
        # deploys created the table without it, so INSERT OR IGNORE never
        # actually deduplicated and a manual refresh could double-insert
        # rows for a given (symbol, scan_date). Dedup first (keep the
        # earliest row per pair, matching the "first scan wins" semantics
        # of INSERT OR IGNORE going forward), then add a unique index —
        # SQLite can't add a constraint via ALTER TABLE, but a unique
        # index is enforced identically by INSERT OR IGNORE.
        conn.execute("""
            DELETE FROM scan_performance
            WHERE id NOT IN (
                SELECT MIN(id) FROM scan_performance
                GROUP BY symbol, scan_date
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_perf_symbol_date
            ON scan_performance(symbol, scan_date)
        """)

        # Watchlist — stocks the user is tracking as potential buys.
        # Questions are stored as a JSON array so a single PATCH can rewrite
        # the whole checklist without juggling FK rows; the volume per row
        # is small (≤30 questions) and we never query across questions.
        # Snapshot captures the V/Q/F scores at add time so the user can
        # see thesis drift on revisit.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                short_name TEXT DEFAULT '',
                thesis TEXT DEFAULT '',
                target_price REAL,
                target_event TEXT DEFAULT '',
                target_date TEXT,
                questions_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'watching',
                notes TEXT DEFAULT '',
                snapshot_json TEXT DEFAULT '{}',
                added_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_status ON watchlist(status)")

        # Portfolio — current holdings the user actually owns. Cash is stored
        # as a synthetic row with symbol='CASH', shares=$amount, cost_basis=1.0
        # so the same CRUD path serves positions and cash without a side table.
        # Market value/P&L are computed at read time from latest prices, not
        # persisted, so they never drift from the live quote.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                short_name TEXT DEFAULT '',
                shares REAL NOT NULL,
                cost_basis REAL NOT NULL,
                notes TEXT DEFAULT '',
                snapshot_json TEXT DEFAULT '{}',
                added_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Dedup table for ntfy alerts. alert_key is the natural key the
        # alert checker constructs (e.g. "price_hit:LULU:130.00" or
        # "catalyst:LULU:2026-06-05:T-1") — uniqueness is what guarantees
        # we send each event exactly once.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist_alerts (
                alert_key TEXT PRIMARY KEY,
                watchlist_id INTEGER,
                kind TEXT NOT NULL,
                sent_at TEXT DEFAULT (datetime('now'))
            )
        """)


def save_scan(result: ScanResult):
    """Persist a scan result, replacing any existing data for that date."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM scan_stocks WHERE scan_date = ?",
            (result.scan_date,),
        )
        conn.execute(
            "DELETE FROM scans WHERE scan_date = ?",
            (result.scan_date,),
        )
        sector_avg_json = json.dumps(
            {k: v.model_dump() for k, v in result.sector_averages.items()}
        )
        market_avg_json = json.dumps(
            {k: v.model_dump() for k, v in result.market_sector_averages.items()}
        )
        conn.execute(
            "INSERT INTO scans (scan_date, scanned_at, total_stocks, sector_averages_json, market_sector_averages_json) VALUES (?, ?, ?, ?, ?)",
            (result.scan_date, result.scanned_at, result.total_stocks, sector_avg_json, market_avg_json),
        )
        conn.executemany(
            "INSERT INTO scan_stocks (scan_date, symbol, data_json, value_score, sector) VALUES (?, ?, ?, ?, ?)",
            [
                (result.scan_date, stock.symbol, stock.model_dump_json(), stock.value_score, stock.sector)
                for stock in result.stocks
            ],
        )


def delete_scan(scan_date: str) -> int:
    """Delete a scan and its associated stocks/performance rows.

    Returns the number of `scans` rows removed (0 or 1). Used to nuke a
    corrupt cached scan — e.g. one saved before the quality-gate guard
    was added, where Yahoo soft-blocked mid-run and every stock has V=0.
    """
    with get_db() as conn:
        conn.execute("DELETE FROM scan_stocks WHERE scan_date = ?", (scan_date,))
        conn.execute("DELETE FROM scan_performance WHERE scan_date = ?", (scan_date,))
        cur = conn.execute("DELETE FROM scans WHERE scan_date = ?", (scan_date,))
        return cur.rowcount


def get_latest_scan() -> Optional[ScanResult]:
    """Get the most recent scan (regardless of quality).

    Use `get_latest_good_scan()` for the user-facing read path — it walks
    back through history if the most recent scan is corrupt (Yahoo
    soft-blocked mid-fetch and produced V=0 across the board). This raw
    version is kept for callers that genuinely want the most recent row
    (premarket refresh, bounce-back base, etc.).
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM scans ORDER BY scan_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return _load_scan(conn, row)


def get_latest_good_scan(min_coverage: float = 0.30) -> Optional[ScanResult]:
    """Most recent scan with at least `min_coverage` of its stocks having
    real fundamentals (non-empty sector AND value_score > 0).

    Walks back through scan history from newest to oldest, returns the
    first scan that clears the bar. If NO scan clears it, falls back to
    the most recent one anyway — better to show degraded data than a
    blank page. The caller can detect this by comparing the returned
    scan_date to the latest known date.

    The quality gate in `main._do_refresh` prevents future low-coverage
    scans from being saved at all, so this fallback only matters for
    legacy corrupt scans (saved before the gate existed) and for
    bootstrap when the very first scan gets saved before the gate
    catches it.
    """
    with get_db() as conn:
        # One pass: per-scan coverage in SQL, ordered newest first.
        rows = conn.execute(
            """
            SELECT scan_date,
                   COUNT(*) AS total,
                   SUM(CASE WHEN sector IS NOT NULL AND TRIM(sector) != ''
                                 AND value_score > 0 THEN 1 ELSE 0 END) AS good
              FROM scan_stocks
             GROUP BY scan_date
             ORDER BY scan_date DESC
             LIMIT 60
            """
        ).fetchall()
        if not rows:
            return None
        for r in rows:
            total = r["total"] or 0
            good = r["good"] or 0
            if total > 0 and (good / total) >= min_coverage:
                scan_row = conn.execute(
                    "SELECT * FROM scans WHERE scan_date = ?", (r["scan_date"],)
                ).fetchone()
                if scan_row:
                    return _load_scan(conn, scan_row)
        # No good scan found — fall back to the absolute latest so the
        # UI still has *something* to render. The status banner from
        # /api/scan/status communicates the degraded state.
        latest_date = rows[0]["scan_date"]
        scan_row = conn.execute(
            "SELECT * FROM scans WHERE scan_date = ?", (latest_date,)
        ).fetchone()
        return _load_scan(conn, scan_row) if scan_row else None


def get_scan_by_date(scan_date: str) -> Optional[ScanResult]:
    """Get scan results for a specific date."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM scans WHERE scan_date = ?", (scan_date,)
        ).fetchone()
        if not row:
            return None
        return _load_scan(conn, row)


def get_latest_scan_averages() -> tuple[dict, dict]:
    """Get sector and market averages from latest scan without loading stocks."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT sector_averages_json, market_sector_averages_json FROM scans ORDER BY scan_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {}, {}
        sec = {k: SectorAverages(**v) for k, v in json.loads(row["sector_averages_json"] or "{}").items()}
        mkt = {k: SectorAverages(**v) for k, v in json.loads(row["market_sector_averages_json"] or "{}").items()}
        return sec, mkt


def get_performance_rows_needing_update() -> list[dict]:
    """Get rows where forward prices need to be filled in."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, symbol, scan_date, price_at_scan,
                   price_15d, price_30d, price_90d, price_180d, price_365d,
                   value_score, quality_score
            FROM scan_performance
            WHERE (price_15d IS NULL AND julianday('now') - julianday(scan_date) >= 15)
               OR (price_30d IS NULL AND julianday('now') - julianday(scan_date) >= 30)
               OR (price_90d IS NULL AND julianday('now') - julianday(scan_date) >= 90)
               OR (price_180d IS NULL AND julianday('now') - julianday(scan_date) >= 180)
               OR (price_365d IS NULL AND julianday('now') - julianday(scan_date) >= 365)
        """).fetchall()
        return [dict(r) for r in rows]


def update_forward_price(row_id: int, days: int, price: float):
    """Fill in a forward price and compute return."""
    with get_db() as conn:
        row = conn.execute("SELECT price_at_scan FROM scan_performance WHERE id = ?", (row_id,)).fetchone()
        if not row or not row["price_at_scan"] or row["price_at_scan"] <= 0:
            return
        ret = round((price - row["price_at_scan"]) / row["price_at_scan"], 4)
        if days == 15:
            conn.execute("UPDATE scan_performance SET price_15d = ?, return_15d = ? WHERE id = ?", (price, ret, row_id))
        elif days == 30:
            conn.execute("UPDATE scan_performance SET price_30d = ?, return_30d = ? WHERE id = ?", (price, ret, row_id))
        elif days == 90:
            conn.execute("UPDATE scan_performance SET price_90d = ?, return_90d = ? WHERE id = ?", (price, ret, row_id))
        elif days == 180:
            conn.execute("UPDATE scan_performance SET price_180d = ?, return_180d = ? WHERE id = ?", (price, ret, row_id))
        elif days == 365:
            conn.execute("UPDATE scan_performance SET price_365d = ?, return_365d = ? WHERE id = ?", (price, ret, row_id))


def get_backtest_summary() -> dict:
    """Compute backtest summary: returns by score tier."""
    with get_db() as conn:
        # Get all rows with at least one forward return
        rows = conn.execute("""
            SELECT value_score, quality_score,
                   return_15d, return_30d, return_90d, return_180d, return_365d,
                   scan_date, symbol, price_at_scan
            FROM scan_performance
            WHERE return_15d IS NOT NULL
               OR return_30d IS NOT NULL
               OR return_90d IS NOT NULL
               OR return_180d IS NOT NULL
               OR return_365d IS NOT NULL
        """).fetchall()

        if not rows:
            return {"has_data": False, "message": "No forward returns computed yet. Returns are filled in 15/30/90/180/365 days after each scan."}

        # Group by value tier
        tiers = {"Strong Value": [], "Moderate Value": [], "Limited Signal": []}
        for r in rows:
            vs = r["value_score"] or 0
            tier = "Strong Value" if vs >= 70 else "Moderate Value" if vs >= 45 else "Limited Signal"
            tiers[tier].append(dict(r))

        def _avg(vals):
            valid = [v for v in vals if v is not None]
            return round(sum(valid) / len(valid) * 100, 2) if valid else None

        summary = {"has_data": True, "total_observations": len(rows), "tiers": {}}
        for tier, stocks in tiers.items():
            summary["tiers"][tier] = {
                "count": len(stocks),
                "avg_return_15d": _avg([s["return_15d"] for s in stocks]),
                "avg_return_30d": _avg([s["return_30d"] for s in stocks]),
                "avg_return_90d": _avg([s["return_90d"] for s in stocks]),
                "avg_return_180d": _avg([s["return_180d"] for s in stocks]),
                "avg_return_365d": _avg([s["return_365d"] for s in stocks]),
            }

        # Also group by quality tier
        q_tiers = {"Quality Buy": [], "Quality Watch": [], "Not Quality": []}
        for r in rows:
            qs = r["quality_score"] or 0
            qt = "Quality Buy" if qs >= 65 else "Quality Watch" if qs >= 45 else "Not Quality"
            q_tiers[qt].append(dict(r))

        summary["quality_tiers"] = {}
        for tier, stocks in q_tiers.items():
            summary["quality_tiers"][tier] = {
                "count": len(stocks),
                "avg_return_15d": _avg([s["return_15d"] for s in stocks]),
                "avg_return_30d": _avg([s["return_30d"] for s in stocks]),
                "avg_return_90d": _avg([s["return_90d"] for s in stocks]),
                "avg_return_180d": _avg([s["return_180d"] for s in stocks]),
                "avg_return_365d": _avg([s["return_365d"] for s in stocks]),
            }

        return summary


def get_backtest_details() -> list[dict]:
    """Get all performance tracking rows with returns for the detail table."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT symbol, scan_date, price_at_scan,
                   price_15d, price_30d, price_90d, price_180d, price_365d,
                   return_15d, return_30d, return_90d, return_180d, return_365d,
                   value_score, quality_score
            FROM scan_performance
            ORDER BY scan_date DESC, value_score DESC
        """).fetchall()
        return [dict(r) for r in rows]


def upsert_latest_price(symbol: str, price: float, updated_at: Optional[str] = None):
    """Record the latest known price for a symbol (UPSERT)."""
    if not symbol or price is None or price <= 0:
        return
    if updated_at is None:
        updated_at = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO symbol_latest_price (symbol, price, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET price = excluded.price, updated_at = excluded.updated_at
        """, (symbol, price, updated_at))


def get_recent_tracked_symbols(lookback_days: int = 365) -> list[str]:
    """Return distinct symbols seen in scan_performance within the last N days.

    Used by the nightly fill job to refresh latest prices for symbols
    that may no longer be at 52W lows but are still being tracked.
    """
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT DISTINCT symbol FROM scan_performance
            WHERE julianday('now') - julianday(scan_date) <= {int(lookback_days)}
        """).fetchall()
        return [r["symbol"] for r in rows]


def get_bounce_back_candidates(threshold_pct: float = 0.05, lookback_days: int = 90) -> list[dict]:
    """Find stocks that hit a 52W low within `lookback_days` and have since
    rebounded by at least `threshold_pct`.

    For each symbol, the "captured low" is the lowest price_at_scan we
    recorded within the lookback window — this represents the deepest dip
    we caught for that symbol. The current price comes from
    symbol_latest_price (refreshed by the fill job and daily scan).

    Returns a list sorted by gain percentage descending.
    """
    with get_db() as conn:
        rows = conn.execute(f"""
            WITH lows AS (
                SELECT symbol, MIN(price_at_scan) AS captured_low
                FROM scan_performance
                WHERE price_at_scan > 0
                  AND julianday('now') - julianday(scan_date) <= {int(lookback_days)}
                GROUP BY symbol
            )
            SELECT
                sp.symbol,
                sp.scan_date AS low_date,
                sp.price_at_scan AS captured_low,
                sp.value_score,
                sp.quality_score,
                lp.price AS current_price,
                lp.updated_at AS current_price_at,
                CAST((julianday('now') - julianday(sp.scan_date)) AS INTEGER) AS days_since_low
            FROM scan_performance sp
            JOIN lows ON lows.symbol = sp.symbol AND lows.captured_low = sp.price_at_scan
            JOIN symbol_latest_price lp ON lp.symbol = sp.symbol
            WHERE lp.price >= sp.price_at_scan * (1.0 + ?)
            GROUP BY sp.symbol
            ORDER BY (lp.price - sp.price_at_scan) / sp.price_at_scan DESC
        """, (threshold_pct,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            low = d["captured_low"]
            cur = d["current_price"]
            d["gain_pct"] = round((cur - low) / low, 4) if low else None
            out.append(d)
        return out


def get_rolling_scores_batch(symbols: list[str]) -> dict:
    """Batch-compute rolling 5-day scores and days_in_scan for all symbols.

    Returns {symbol: {rolling_value, rolling_quality, days}} in one query.
    """
    if not symbols:
        return {}
    with get_db() as conn:
        # Get the last 5 scan dates
        dates = conn.execute(
            "SELECT DISTINCT scan_date FROM scan_stocks ORDER BY scan_date DESC LIMIT 5"
        ).fetchall()
        if not dates:
            return {}
        date_list = [d["scan_date"] for d in dates]
        placeholders = ",".join("?" * len(date_list))

        rows = conn.execute(f"""
            SELECT symbol,
                   AVG(value_score) as avg_value,
                   COUNT(*) as days,
                   AVG(CAST(json_extract(data_json, '$.quality_score') AS REAL)) as avg_quality
            FROM scan_stocks
            WHERE scan_date IN ({placeholders})
            GROUP BY symbol
        """, date_list).fetchall()

        return {
            r["symbol"]: {
                "rolling_value": round(r["avg_value"]) if r["avg_value"] else 0,
                "rolling_quality": round(r["avg_quality"]) if r["avg_quality"] else 0,
                "days": r["days"],
            }
            for r in rows
        }


def get_stock_history(symbol: str) -> list[dict]:
    """Get score history for a single stock across all scan dates."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT scan_date, data_json, value_score FROM scan_stocks WHERE symbol = ? ORDER BY scan_date DESC",
            (symbol.upper(),),
        ).fetchall()
        results = []
        for r in rows:
            data = json.loads(r["data_json"])
            results.append({
                "scan_date": r["scan_date"],
                "value_score": r["value_score"],
                "quality_score": data.get("quality_score", 0),
                "price": data.get("price", 0),
                "score_tier": data.get("score_tier", ""),
                "quality_tier": data.get("quality_tier", ""),
                "score_reasons": data.get("score_reasons", []),
            })
        return results


def save_performance_tracking(scan_date: str, stocks: list):
    """Save performance tracking rows for future return calculation.

    Also seeds symbol_latest_price for every stock in the scan — they're
    at a 52W low today, but tomorrow if they bounce we want a fresh
    baseline price to compare against.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        for s in stocks:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO scan_performance (symbol, scan_date, price_at_scan, value_score, quality_score) VALUES (?, ?, ?, ?, ?)",
                    (s.symbol, scan_date, s.price, s.value_score, s.quality_score),
                )
                if s.price and s.price > 0:
                    conn.execute("""
                        INSERT INTO symbol_latest_price (symbol, price, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(symbol) DO UPDATE SET price = excluded.price, updated_at = excluded.updated_at
                    """, (s.symbol, float(s.price), now_iso))
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Performance tracking insert failed for {s.symbol}: {e}")


def get_scan_history() -> list[ScanHistoryEntry]:
    """Get list of all available scan dates."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT scan_date, scanned_at, total_stocks FROM scans ORDER BY scan_date DESC"
        ).fetchall()
        return [
            ScanHistoryEntry(
                scan_date=r["scan_date"],
                scanned_at=r["scanned_at"],
                total_stocks=r["total_stocks"],
            )
            for r in rows
        ]


def _load_scan(conn: sqlite3.Connection, scan_row: sqlite3.Row) -> ScanResult:
    stock_rows = conn.execute(
        "SELECT data_json FROM scan_stocks WHERE scan_date = ? ORDER BY value_score DESC",
        (scan_row["scan_date"],),
    ).fetchall()

    stocks = [ScoredStock.model_validate_json(r["data_json"]) for r in stock_rows]

    raw_avgs = json.loads(scan_row["sector_averages_json"] or "{}")
    sector_averages = {k: SectorAverages(**v) for k, v in raw_avgs.items()}

    raw_market = json.loads(scan_row["market_sector_averages_json"] or "{}")
    market_sector_averages = {k: SectorAverages(**v) for k, v in raw_market.items()}

    return ScanResult(
        scan_date=scan_row["scan_date"],
        scanned_at=scan_row["scanned_at"],
        total_stocks=scan_row["total_stocks"],
        stocks=stocks,
        sector_averages=sector_averages,
        market_sector_averages=market_sector_averages,
    )


# ─────────────────────── Watchlist (Potential Buys) ───────────────────────
# CRUD helpers. Rows are dicts (not Pydantic models) so the API layer can
# shape them freely — the watchlist surface is small and self-contained.

def _watchlist_row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id":            r["id"],
        "symbol":        r["symbol"],
        "short_name":    r["short_name"] or "",
        "thesis":        r["thesis"] or "",
        "target_price":  r["target_price"],
        "target_event":  r["target_event"] or "",
        "target_date":   r["target_date"],
        "questions":     json.loads(r["questions_json"] or "[]"),
        "status":        r["status"] or "watching",
        "notes":         r["notes"] or "",
        "snapshot":      json.loads(r["snapshot_json"] or "{}"),
        "added_at":      r["added_at"],
        "updated_at":    r["updated_at"],
    }


def list_watchlist() -> list[dict]:
    """All watchlist items, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist ORDER BY "
            "CASE status WHEN 'watching' THEN 0 WHEN 'bought' THEN 1 ELSE 2 END, "
            "added_at DESC"
        ).fetchall()
        return [_watchlist_row_to_dict(r) for r in rows]


def get_watchlist_item(item_id: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM watchlist WHERE id = ?", (item_id,)).fetchone()
        return _watchlist_row_to_dict(r) if r else None


def get_watchlist_by_symbol(symbol: str) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
        return _watchlist_row_to_dict(r) if r else None


def create_watchlist_item(
    symbol: str,
    short_name: str = "",
    thesis: str = "",
    target_price: Optional[float] = None,
    target_event: str = "",
    target_date: Optional[str] = None,
    questions: Optional[list[dict]] = None,
    notes: str = "",
    snapshot: Optional[dict] = None,
) -> dict:
    """Insert a new watchlist row. Raises sqlite3.IntegrityError on duplicate symbol."""
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO watchlist
               (symbol, short_name, thesis, target_price, target_event,
                target_date, questions_json, notes, snapshot_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                symbol,
                short_name or "",
                thesis or "",
                target_price,
                target_event or "",
                target_date,
                json.dumps(questions or []),
                notes or "",
                json.dumps(snapshot or {}),
            ),
        )
        new_id = cur.lastrowid
    return get_watchlist_item(new_id)


# Whitelist of fields that PATCH /api/watchlist/{id} can update. Anything
# else in the body is ignored. `questions` and `snapshot` are JSON-serialized
# before persisting; other fields go in raw.
_WATCHLIST_UPDATABLE = {
    "short_name", "thesis", "target_price", "target_event", "target_date",
    "status", "notes",
}


def update_watchlist_item(item_id: int, fields: dict) -> Optional[dict]:
    """Partial update. Returns the updated row, or None if not found."""
    sets, vals = [], []
    for k, v in fields.items():
        if k in _WATCHLIST_UPDATABLE:
            sets.append(f"{k} = ?")
            vals.append(v)
        elif k == "questions":
            sets.append("questions_json = ?")
            vals.append(json.dumps(v or []))
        elif k == "snapshot":
            sets.append("snapshot_json = ?")
            vals.append(json.dumps(v or {}))
    if not sets:
        return get_watchlist_item(item_id)
    sets.append("updated_at = datetime('now')")
    vals.append(item_id)
    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE watchlist SET {', '.join(sets)} WHERE id = ?", vals
        )
        if cur.rowcount == 0:
            return None
    return get_watchlist_item(item_id)


def delete_watchlist_item(item_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM watchlist WHERE id = ?", (item_id,))
        return cur.rowcount > 0


# ── Watchlist alert dedup ────────────────────────────────────────────
# alert_key uniqueness guarantees once-and-only-once delivery per event.

def alert_already_sent(alert_key: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM watchlist_alerts WHERE alert_key = ?", (alert_key,)
        ).fetchone()
        return row is not None


def record_alert_sent(alert_key: str, watchlist_id: int, kind: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist_alerts (alert_key, watchlist_id, kind) "
            "VALUES (?, ?, ?)",
            (alert_key, watchlist_id, kind),
        )


def reset_price_alert(symbol: str, target_price: float) -> int:
    """Clear the price-hit dedup row when price has risen back ≥3% above
    target. Lets the alert re-fire on the next dip — prevents one whippy
    intraday wiggle from silencing the alert forever."""
    key_prefix = f"price_hit:{symbol}:"
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM watchlist_alerts WHERE alert_key LIKE ? AND kind = 'price_hit'",
            (key_prefix + "%",),
        )
        return cur.rowcount


# ─────────────────────── Portfolio (Current Holdings) ───────────────────────
# Same CRUD shape as watchlist. Cash is a synthetic row (symbol='CASH',
# shares=$, cost_basis=1.0). API layer enriches each row with current price,
# market value, and P&L; we never persist computed fields so they stay
# consistent with whatever the latest quote is.

def _portfolio_row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id":         r["id"],
        "symbol":     r["symbol"],
        "short_name": r["short_name"] or "",
        "shares":     r["shares"],
        "cost_basis": r["cost_basis"],
        "notes":      r["notes"] or "",
        "snapshot":   json.loads(r["snapshot_json"] or "{}"),
        "added_at":   r["added_at"],
        "updated_at": r["updated_at"],
    }


def list_portfolio() -> list[dict]:
    """All portfolio rows. CASH first, then positions by largest position size."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio ORDER BY "
            "CASE WHEN symbol = 'CASH' THEN 0 ELSE 1 END, "
            "(shares * cost_basis) DESC"
        ).fetchall()
        return [_portfolio_row_to_dict(r) for r in rows]


def get_portfolio_item(item_id: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM portfolio WHERE id = ?", (item_id,)).fetchone()
        return _portfolio_row_to_dict(r) if r else None


def get_portfolio_by_symbol(symbol: str) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM portfolio WHERE symbol = ?", (symbol,)).fetchone()
        return _portfolio_row_to_dict(r) if r else None


def create_portfolio_item(
    symbol: str,
    shares: float,
    cost_basis: float,
    short_name: str = "",
    notes: str = "",
    snapshot: Optional[dict] = None,
) -> dict:
    """Insert a new portfolio row. Raises sqlite3.IntegrityError on duplicate symbol."""
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO portfolio
               (symbol, short_name, shares, cost_basis, notes, snapshot_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                symbol,
                short_name or "",
                shares,
                cost_basis,
                notes or "",
                json.dumps(snapshot or {}),
            ),
        )
        new_id = cur.lastrowid
    return get_portfolio_item(new_id)


_PORTFOLIO_UPDATABLE = {"short_name", "shares", "cost_basis", "notes"}


def update_portfolio_item(item_id: int, fields: dict) -> Optional[dict]:
    """Partial update. Returns the updated row, or None if not found."""
    sets, vals = [], []
    for k, v in fields.items():
        if k in _PORTFOLIO_UPDATABLE:
            sets.append(f"{k} = ?")
            vals.append(v)
        elif k == "snapshot":
            sets.append("snapshot_json = ?")
            vals.append(json.dumps(v or {}))
    if not sets:
        return get_portfolio_item(item_id)
    sets.append("updated_at = datetime('now')")
    vals.append(item_id)
    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE portfolio SET {', '.join(sets)} WHERE id = ?", vals
        )
        if cur.rowcount == 0:
            return None
    return get_portfolio_item(item_id)


def delete_portfolio_item(item_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM portfolio WHERE id = ?", (item_id,))
        return cur.rowcount > 0
