"""Daily watchlist alert checker — runs as a standalone job.

Two alert kinds:
  • price_hit  — current_price <= target_price (with 3% rebound dedup)
  • catalyst   — target_date - N days (default: T-1 and T-0)

Idempotent: dedup keys in `watchlist_alerts` ensure each event fires once.
Wired by launchd (~/Library/LaunchAgents/com.valueinvest52.alerts.plist)
to run every morning at 9am Pacific. Safe to run manually any time:

    python3 -m server.check_watchlist_alerts
    python3 -m server.check_watchlist_alerts --test    # send test ping only
    python3 -m server.check_watchlist_alerts --dry-run # log what would send

Exit codes: 0 always (so launchd doesn't flag it red on a network blip).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

from server import database as db
from server.config import WATCHLIST_CATALYST_LEAD_DAYS
from server.notifications import send_ntfy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Rebound threshold: once a price-hit alert fires, suppress repeats until
# price climbs back ≥ this fraction above target. Then on the next dip,
# we let the alert re-arm. 3% covers normal intraday wiggle without
# missing genuine re-entries.
REBOUND_FRACTION = 0.03

DASHBOARD_URL = "http://127.0.0.1:8000"


def _fetch_current_price(symbol: str) -> float | None:
    """Try the local cached price first, fall back to yfinance fast_info."""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT price FROM symbol_latest_price WHERE symbol = ?", (symbol,)
        ).fetchone()
    if row and row["price"]:
        return float(row["price"])
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        # fast_info avoids a heavy quote-summary fetch
        return float(t.fast_info["last_price"])
    except Exception as e:
        logger.warning(f"price fetch failed for {symbol}: {e}")
        return None


def _check_price(item: dict, dry_run: bool) -> int:
    """Returns count of alerts sent (0 or 1)."""
    sym = item["symbol"]
    tgt = item.get("target_price")
    if tgt is None:
        return 0

    cur = _fetch_current_price(sym)
    if cur is None:
        return 0

    # If price has rebounded well above target, clear any prior price_hit
    # dedup row so a future dip re-arms the alert.
    if cur >= tgt * (1 + REBOUND_FRACTION):
        cleared = db.reset_price_alert(sym, tgt)
        if cleared:
            logger.info(f"{sym}: rebounded to ${cur:.2f} ≥ target+{int(REBOUND_FRACTION*100)}% — re-armed price alert")

    if cur > tgt:
        return 0

    # Round target to 2dp in the key so reseting target_price by ±$0.01
    # via a typo doesn't bypass dedup.
    key = f"price_hit:{sym}:{tgt:.2f}"
    if db.alert_already_sent(key):
        logger.info(f"{sym}: price ${cur:.2f} ≤ ${tgt:.2f} — already alerted")
        return 0

    title = f"{sym} hit ${cur:.2f} — at buy zone"
    msg = (
        f"{sym} ({item.get('short_name','')}) is at ${cur:.2f}, "
        f"at or below your buy point of ${tgt:.2f}.\n\n"
        f"Time to walk the {len(item.get('questions') or [])} DD questions."
    )
    if dry_run:
        logger.info(f"[dry-run] would send: {title}")
        return 1
    if send_ntfy(title, msg, priority=4, tags=["chart_with_upwards_trend"], click_url=DASHBOARD_URL):
        db.record_alert_sent(key, item["id"], "price_hit")
        return 1
    return 0


def _check_catalyst(item: dict, today: date, dry_run: bool) -> int:
    """Returns count of catalyst reminders sent."""
    sym = item["symbol"]
    td_str = item.get("target_date")
    if not td_str:
        return 0
    try:
        td = datetime.strptime(td_str, "%Y-%m-%d").date()
    except ValueError:
        logger.warning(f"{sym}: bad target_date '{td_str}'")
        return 0

    sent = 0
    for lead in WATCHLIST_CATALYST_LEAD_DAYS:
        if today != td - timedelta(days=lead):
            continue
        # Lead is part of the key so T-1 and T-0 are independently dedup'd.
        key = f"catalyst:{sym}:{td_str}:T-{lead}"
        if db.alert_already_sent(key):
            continue
        when = "today" if lead == 0 else f"in {lead} day{'s' if lead != 1 else ''}"
        event = item.get("target_event") or "catalyst event"
        title = f"{sym} {event} — {when}"
        msg = (
            f"{sym} ({item.get('short_name','')}): {event} {when} ({td_str}).\n\n"
            f"Pull the print and walk your DD questions when it's out."
        )
        if dry_run:
            logger.info(f"[dry-run] would send: {title}")
            sent += 1
            continue
        if send_ntfy(title, msg, priority=4, tags=["calendar"], click_url=DASHBOARD_URL):
            db.record_alert_sent(key, item["id"], "catalyst")
            sent += 1
    return sent


def run(dry_run: bool = False) -> int:
    items = [i for i in db.list_watchlist() if i.get("status") == "watching"]
    if not items:
        logger.info("no watching items — nothing to check")
        return 0
    today = date.today()
    sent = 0
    for item in items:
        sent += _check_price(item, dry_run)
        sent += _check_catalyst(item, today, dry_run)
    logger.info(f"checked {len(items)} items, sent {sent} alert(s)")
    return sent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="log what would send, don't send")
    p.add_argument("--test", action="store_true", help="send a test ping and exit")
    args = p.parse_args()

    db.init_db()  # ensure tables exist (first-run safety)

    if args.test:
        ok = send_ntfy(
            "ValueInvest52Lows test ping",
            "If you're reading this, ntfy is wired up correctly. 🎉",
            priority=4,
            tags=["white_check_mark"],
            click_url=DASHBOARD_URL,
        )
        print("sent" if ok else "failed")
        sys.exit(0)

    try:
        run(dry_run=args.dry_run)
    except Exception as e:
        logger.exception(f"alert run crashed: {e}")
    sys.exit(0)


if __name__ == "__main__":
    main()
