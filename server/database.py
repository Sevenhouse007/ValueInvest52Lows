"""SQLite database operations for persisting scan results."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
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
                price_30d REAL,
                price_90d REAL,
                price_180d REAL,
                return_30d REAL,
                return_90d REAL,
                return_180d REAL,
                value_score INTEGER,
                quality_score INTEGER,
                UNIQUE(scan_date, symbol)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_perf_symbol ON scan_performance(symbol)")

        # Migration: add market_sector_averages_json if not present
        try:
            conn.execute("SELECT market_sector_averages_json FROM scans LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE scans ADD COLUMN market_sector_averages_json TEXT DEFAULT '{}'")


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


def get_latest_scan() -> Optional[ScanResult]:
    """Get the most recent scan."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM scans ORDER BY scan_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return _load_scan(conn, row)


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
    """Save performance tracking rows for future return calculation."""
    with get_db() as conn:
        for s in stocks:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO scan_performance (symbol, scan_date, price_at_scan, value_score, quality_score) VALUES (?, ?, ?, ?, ?)",
                    (s.symbol, scan_date, s.price, s.value_score, s.quality_score),
                )
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
