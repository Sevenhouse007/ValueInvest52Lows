"""SQLite database operations for persisting scan results."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
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

        # Migration: add prev_close so the portfolio/watchlist endpoints can
        # surface daily $ and % change without a second yfinance roundtrip.
        # quoteSummary returns regularMarketPreviousClose alongside the live
        # price; we cache it here next to the existing price column so reads
        # stay fast even when a position isn't in the daily-scan universe.
        try:
            conn.execute("SELECT prev_close FROM symbol_latest_price LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN prev_close REAL")

        # Migration: add mos_score (0-100, computed daily) + mos_updated_at so
        # the UI can render a numeric Margin of Safety subscore alongside the
        # categorical pill. Categorical lives on snapshot.margin_of_safety
        # (manual override); this column is the deterministic auto-refreshed
        # subscore, so the UI can surface drift between the two.
        try:
            conn.execute("SELECT mos_score FROM symbol_latest_price LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_score INTEGER")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_updated_at TEXT")

        # Migration: store the 3 subscores so the UI can show the breakdown
        # (Cheapness / Quality / Capital-Return) when the user wants to see
        # WHY the composite is what it is. All three are 0-N integers; their
        # max sums to 100.
        try:
            conn.execute("SELECT mos_cheapness FROM symbol_latest_price LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_cheapness INTEGER")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_quality INTEGER")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_capreturn INTEGER")

        # Migration: Klarman-style MoS components. mos_score is now the
        # composite *percentage discount to intrinsic value* (Seth Klarman's
        # definition: MoS = (intrinsic - price) / intrinsic). Positive = the
        # stock trades below estimated intrinsic value; negative = premium.
        # The three components are themselves discount percentages so the
        # composite is just a weighted average. mos_intrinsic stores the
        # implied per-share fair value so the UI can show "fair $X vs price
        # $Y" alongside the percentage.
        try:
            conn.execute("SELECT mos_peer_discount FROM symbol_latest_price LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_peer_discount INTEGER")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_dcf_discount INTEGER")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_asset_coverage INTEGER")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_intrinsic REAL")

        # Migration: scenario-analysis DCF — store the bull/base/bear
        # intrinsic-per-share values so the UI tooltip can show the full
        # dispersion. mos_intrinsic above now holds the weighted middle
        # (25% bull + 50% base + 25% bear); these three columns hold the
        # raw scenarios so the user can see how wide the band is.
        try:
            conn.execute("SELECT mos_dcf_bull FROM symbol_latest_price LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_dcf_bull REAL")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_dcf_base REAL")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_dcf_bear REAL")

        # Migration: data-quality flag for the MoS composite. Indicates how
        # many of the 3 axes (peer / DCF / asset) actually contributed and
        # whether the DCF band was tight or wide. UI surfaces a ⚠️ icon
        # when quality is "low" (single-axis, e.g. banks where Yahoo doesn't
        # report FCF or balance-sheet detail) so the score isn't taken at
        # face value.
        try:
            conn.execute("SELECT mos_quality_flag FROM symbol_latest_price LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_quality_flag TEXT")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_axes_used TEXT")

        # Migration: business-classification + valuation-method labels so
        # the UI can show "DCF" vs "Fair P/B" vs "Mid-cycle EV/EBITDA" etc.
        # This is purely cosmetic — composite math doesn't depend on it —
        # but it lets the user verify the right framework is being used.
        try:
            conn.execute("SELECT mos_method FROM symbol_latest_price LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_method TEXT")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_business TEXT")

        # MoS history — daily snapshots of (symbol, mos_score, intrinsic,
        # price_at_snapshot) for portfolio + watchlist symbols. Used to
        # validate whether MoS readings predict forward returns: each
        # snapshot is paired with the price N days later to compute
        # forward returns. Klarman emphasizes track-record validation —
        # without this, we don't know if the framework actually works.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mos_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                mos_score INTEGER,
                raw_score INTEGER,
                intrinsic REAL,
                price_at_snapshot REAL,
                business TEXT,
                method TEXT,
                quality_flag TEXT,
                price_15d REAL,
                price_30d REAL,
                price_90d REAL,
                price_180d REAL,
                return_15d REAL,
                return_30d REAL,
                return_90d REAL,
                return_180d REAL,
                UNIQUE(symbol, snapshot_date)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mos_history_date ON mos_history(snapshot_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mos_history_symbol ON mos_history(symbol)")

        # Migration: Klarman-style composite adjustments —
        #   raw_score        = composite before adjustments (peer/DCF/asset only)
        #   buyback_credit   = +pp added for shareholder yield via buybacks
        #   quality_penalty  = -pp subtracted for accounting/distress flags
        #   quality_reasons  = JSON list explaining the penalty
        #   implied_growth   = reverse-DCF growth rate that justifies current price
        # Net composite = raw_score + buyback_credit - quality_penalty
        try:
            conn.execute("SELECT mos_raw_score FROM symbol_latest_price LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_raw_score INTEGER")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_buyback_credit REAL")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_quality_penalty REAL")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_quality_reasons TEXT")
            conn.execute("ALTER TABLE symbol_latest_price ADD COLUMN mos_implied_growth REAL")

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


def upsert_latest_price(
    symbol: str,
    price: float,
    updated_at: Optional[str] = None,
    prev_close: Optional[float] = None,
):
    """Record the latest known price for a symbol (UPSERT). When prev_close
    is provided it's persisted too so day-change math comes from cache; when
    omitted, any existing prev_close is preserved (the daily scan only knows
    the latest scan price, not the prior close)."""
    if not symbol or price is None or price <= 0:
        return
    if updated_at is None:
        updated_at = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        if prev_close is not None and prev_close > 0:
            conn.execute("""
                INSERT INTO symbol_latest_price (symbol, price, updated_at, prev_close)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    price = excluded.price,
                    updated_at = excluded.updated_at,
                    prev_close = excluded.prev_close
            """, (symbol, price, updated_at, prev_close))
        else:
            # Don't clobber an existing prev_close just because the caller
            # didn't have it (e.g. legacy daily-scan path).
            conn.execute("""
                INSERT INTO symbol_latest_price (symbol, price, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    price = excluded.price,
                    updated_at = excluded.updated_at
            """, (symbol, price, updated_at))


def upsert_mos_score(
    symbol: str,
    mos_score: Optional[int],
    peer_discount: Optional[int] = None,
    dcf_discount: Optional[int] = None,
    asset_coverage: Optional[int] = None,
    intrinsic: Optional[float] = None,
    dcf_bull: Optional[float] = None,
    dcf_base: Optional[float] = None,
    dcf_bear: Optional[float] = None,
    quality_flag: Optional[str] = None,
    axes_used: Optional[list] = None,
    method: Optional[str] = None,
    business: Optional[str] = None,
    raw_score: Optional[int] = None,
    buyback_credit: Optional[float] = None,
    quality_penalty: Optional[float] = None,
    quality_reasons: Optional[list] = None,
    implied_growth: Optional[float] = None,
) -> None:
    """Persist the Klarman-style MoS = % discount to intrinsic value.

    quality_flag  = 'high' | 'medium' | 'low' — confidence in the composite
    axes_used     = which of [peer, dcf, asset] actually contributed (json)
    method        = label of valuation framework ("DCF", "Fair P/B", etc.)
    business      = sector bucket (bank/managed_care/energy_commodity/...)
    """
    if not symbol:
        return
    updated_at = datetime.now(timezone.utc).isoformat()
    axes_json = json.dumps(axes_used) if axes_used is not None else None
    reasons_json = json.dumps(quality_reasons) if quality_reasons else None
    with get_db() as conn:
        conn.execute("""
            INSERT INTO symbol_latest_price (
                symbol, price, updated_at, mos_score, mos_updated_at,
                mos_peer_discount, mos_dcf_discount, mos_asset_coverage, mos_intrinsic,
                mos_dcf_bull, mos_dcf_base, mos_dcf_bear,
                mos_quality_flag, mos_axes_used, mos_method, mos_business,
                mos_raw_score, mos_buyback_credit, mos_quality_penalty,
                mos_quality_reasons, mos_implied_growth
            )
            VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                mos_score = excluded.mos_score,
                mos_updated_at = excluded.mos_updated_at,
                mos_peer_discount = excluded.mos_peer_discount,
                mos_dcf_discount = excluded.mos_dcf_discount,
                mos_asset_coverage = excluded.mos_asset_coverage,
                mos_intrinsic = excluded.mos_intrinsic,
                mos_dcf_bull = excluded.mos_dcf_bull,
                mos_dcf_base = excluded.mos_dcf_base,
                mos_dcf_bear = excluded.mos_dcf_bear,
                mos_quality_flag = excluded.mos_quality_flag,
                mos_axes_used = excluded.mos_axes_used,
                mos_method = excluded.mos_method,
                mos_business = excluded.mos_business,
                mos_raw_score = excluded.mos_raw_score,
                mos_buyback_credit = excluded.mos_buyback_credit,
                mos_quality_penalty = excluded.mos_quality_penalty,
                mos_quality_reasons = excluded.mos_quality_reasons,
                mos_implied_growth = excluded.mos_implied_growth
        """, (symbol, updated_at, mos_score, updated_at,
              peer_discount, dcf_discount, asset_coverage, intrinsic,
              dcf_bull, dcf_base, dcf_bear,
              quality_flag, axes_json, method, business,
              raw_score, buyback_credit, quality_penalty,
              reasons_json, implied_growth))


def get_mos_score(symbol: str) -> Optional[dict]:
    """Read the stored Klarman MoS composite + 3 components + scenario DCFs
    + quality flag + axes used."""
    with get_db() as conn:
        r = conn.execute(
            "SELECT mos_score, mos_updated_at, mos_peer_discount, mos_dcf_discount, "
            "mos_asset_coverage, mos_intrinsic, mos_dcf_bull, mos_dcf_base, mos_dcf_bear, "
            "mos_quality_flag, mos_axes_used, mos_method, mos_business, "
            "mos_raw_score, mos_buyback_credit, mos_quality_penalty, "
            "mos_quality_reasons, mos_implied_growth "
            "FROM symbol_latest_price WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if not r:
            return None
        axes = None
        if r["mos_axes_used"]:
            try:
                axes = json.loads(r["mos_axes_used"])
            except Exception:
                axes = None
        reasons = None
        if r["mos_quality_reasons"]:
            try:
                reasons = json.loads(r["mos_quality_reasons"])
            except Exception:
                reasons = None
        return {
            "mos_score": r["mos_score"],
            "mos_updated_at": r["mos_updated_at"],
            "mos_peer_discount": r["mos_peer_discount"],
            "mos_dcf_discount": r["mos_dcf_discount"],
            "mos_asset_coverage": r["mos_asset_coverage"],
            "mos_intrinsic": r["mos_intrinsic"],
            "mos_dcf_bull": r["mos_dcf_bull"],
            "mos_dcf_base": r["mos_dcf_base"],
            "mos_dcf_bear": r["mos_dcf_bear"],
            "mos_quality_flag": r["mos_quality_flag"],
            "mos_axes_used": axes,
            "mos_method": r["mos_method"],
            "mos_business": r["mos_business"],
            "mos_raw_score": r["mos_raw_score"],
            "mos_buyback_credit": r["mos_buyback_credit"],
            "mos_quality_penalty": r["mos_quality_penalty"],
            "mos_quality_reasons": reasons,
            "mos_implied_growth": r["mos_implied_growth"],
        }


def insert_mos_snapshot(
    symbol: str,
    snapshot_date: str,  # YYYY-MM-DD
    mos_score: Optional[int],
    raw_score: Optional[int],
    intrinsic: Optional[float],
    price_at_snapshot: Optional[float],
    business: Optional[str] = None,
    method: Optional[str] = None,
    quality_flag: Optional[str] = None,
) -> None:
    """Record a daily MoS reading for forward-return tracking. UNIQUE
    constraint on (symbol, snapshot_date) means re-running the recompute
    same day overwrites the previous snapshot rather than accumulating."""
    if not symbol or mos_score is None:
        return
    with get_db() as conn:
        conn.execute("""
            INSERT INTO mos_history
                (symbol, snapshot_date, mos_score, raw_score, intrinsic,
                 price_at_snapshot, business, method, quality_flag)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, snapshot_date) DO UPDATE SET
                mos_score = excluded.mos_score,
                raw_score = excluded.raw_score,
                intrinsic = excluded.intrinsic,
                price_at_snapshot = excluded.price_at_snapshot,
                business = excluded.business,
                method = excluded.method,
                quality_flag = excluded.quality_flag
        """, (symbol, snapshot_date, mos_score, raw_score, intrinsic,
              price_at_snapshot, business, method, quality_flag))


def get_mos_history_needing_fill(window_days: int) -> list[dict]:
    """Return mos_history rows whose snapshot_date is exactly window_days
    ago (or older) and whose price_{window}d hasn't been filled yet. Used
    by the daily forward-fill job to compute realized returns once enough
    time has passed."""
    col_price = f"price_{window_days}d"
    col_return = f"return_{window_days}d"
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT id, symbol, snapshot_date, price_at_snapshot
            FROM mos_history
            WHERE snapshot_date <= ?
              AND {col_price} IS NULL
              AND price_at_snapshot IS NOT NULL
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]


def fill_mos_forward_return(history_id: int, window_days: int, price: float) -> None:
    """Once we know the price N days after a snapshot, fill the price_{N}d
    and return_{N}d columns. Called by the daily forward-fill job."""
    if not price or price <= 0:
        return
    col_price = f"price_{window_days}d"
    col_return = f"return_{window_days}d"
    with get_db() as conn:
        # Get the snapshot price for return calc
        r = conn.execute(
            f"SELECT price_at_snapshot FROM mos_history WHERE id = ?",
            (history_id,),
        ).fetchone()
        if not r or not r["price_at_snapshot"]:
            return
        ret = (price - r["price_at_snapshot"]) / r["price_at_snapshot"]
        conn.execute(
            f"UPDATE mos_history SET {col_price} = ?, {col_return} = ? WHERE id = ?",
            (price, ret, history_id),
        )


def get_mos_backtest_summary() -> dict:
    """Bucket MoS history by score and report average forward returns per
    bucket. Klarman's zones:
      Klarman zone   ≥ +30%   (deep margin of safety)
      Modest cushion +10..30%
      Fair value     -10..+10%
      Slight premium -10..-30%
      Heavy premium  ≤ -30%
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT mos_score, business,
                   return_15d, return_30d, return_90d, return_180d
            FROM mos_history
            WHERE mos_score IS NOT NULL
              AND (return_15d IS NOT NULL
                   OR return_30d IS NOT NULL
                   OR return_90d IS NOT NULL
                   OR return_180d IS NOT NULL)
        """).fetchall()

        total_snapshots = conn.execute(
            "SELECT COUNT(*) FROM mos_history"
        ).fetchone()[0]

        if not rows:
            # No matured snapshots yet — show progress so user knows when
            # to expect first results.
            symbol_count = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM mos_history"
            ).fetchone()[0]
            oldest = conn.execute(
                "SELECT MIN(snapshot_date) FROM mos_history"
            ).fetchone()[0]
            days_collected = 0
            if oldest:
                days_collected = (date.today() - date.fromisoformat(oldest)).days
            return {
                "has_data": False,
                "total_snapshots": total_snapshots,
                "unique_symbols": symbol_count,
                "days_collected": days_collected,
                "next_window": "15 days" if days_collected < 15 else "30 days" if days_collected < 30 else "90 days",
                "message": (
                    f"Collecting MoS history — {total_snapshots} snapshots across "
                    f"{symbol_count} symbols over {days_collected} days. "
                    f"First validated returns available after 15 days; "
                    f"meaningful sample after 30+ days."
                ),
            }

        def bucket_of(score: int) -> str:
            if score >= 30: return "Klarman zone (≥30%)"
            if score >= 10: return "Modest cushion (10–30%)"
            if score >= -10: return "Fair value (-10 to 10%)"
            if score >= -30: return "Slight premium (-30 to -10%)"
            return "Heavy premium (<-30%)"

        buckets: dict[str, list[dict]] = {}
        for r in rows:
            b = bucket_of(r["mos_score"])
            buckets.setdefault(b, []).append(dict(r))

        def _avg(vals):
            valid = [v for v in vals if v is not None]
            return round(sum(valid) / len(valid) * 100, 2) if valid else None

        result = {
            "has_data": True,
            "total_snapshots": total_snapshots,
            "matured_observations": len(rows),
            "buckets": {},
        }
        bucket_order = [
            "Klarman zone (≥30%)", "Modest cushion (10–30%)", "Fair value (-10 to 10%)",
            "Slight premium (-30 to -10%)", "Heavy premium (<-30%)",
        ]
        for b in bucket_order:
            stocks = buckets.get(b, [])
            result["buckets"][b] = {
                "count": len(stocks),
                "avg_return_15d": _avg([s["return_15d"] for s in stocks]),
                "avg_return_30d": _avg([s["return_30d"] for s in stocks]),
                "avg_return_90d": _avg([s["return_90d"] for s in stocks]),
                "avg_return_180d": _avg([s["return_180d"] for s in stocks]),
            }
        return result


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
