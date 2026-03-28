"""Daily digest notifications — email (SMTP) and Slack webhook."""

from __future__ import annotations

import json
import logging
from typing import Optional

from server.config import (
    NOTIFY_ENABLED, NOTIFY_FROM, NOTIFY_TO, NOTIFY_TOP_N,
    SLACK_WEBHOOK_URL, SMTP_HOST, SMTP_PASS, SMTP_PORT, SMTP_USER,
)

logger = logging.getLogger(__name__)


def send_daily_digest(scan_date: str, stocks: list, prev_symbols: Optional[set] = None):
    """Send daily digest of top new/improved picks. Called after scan completes."""
    if not NOTIFY_ENABLED:
        return

    prev = prev_symbols or set()

    # Filter: new stocks or score improved significantly
    candidates = []
    for s in stocks:
        is_new = s.symbol not in prev
        if is_new or s.value_score >= 70 or s.quality_score >= 65:
            candidates.append(s)

    top = sorted(candidates, key=lambda s: s.value_score + s.quality_score, reverse=True)[:NOTIFY_TOP_N]

    if not top:
        logger.info("No notable picks today — skipping digest")
        return

    strong_count = sum(1 for s in stocks if s.value_score >= 70)
    quality_count = sum(1 for s in stocks if s.quality_score >= 65)

    subject = f"52W Low Scanner — {len(top)} top picks · {scan_date}"
    body = f"""Daily Scan: {scan_date}
Total stocks scanned: {len(stocks)}
Strong Value (≥70): {strong_count}
Quality Buy (≥65): {quality_count}

TOP PICKS
---------
"""
    for i, s in enumerate(top, 1):
        reasons = s.score_reasons[:3] if s.score_reasons else []
        body += f"""
{i}. {s.symbol} — {s.short_name}
   Sector: {s.sector}
   Value: {s.value_score} ({s.score_tier}) | Quality: {s.quality_score} ({s.quality_tier})
   Price: ${s.price:.2f}
   Signals: {', '.join(reasons)}
"""

    body += "\n---\nAutomated scan. Not investment advice.\n"

    # Send email
    if SMTP_HOST and NOTIFY_TO:
        _send_email(subject, body)

    # Send Slack
    if SLACK_WEBHOOK_URL:
        _send_slack(top, scan_date)


def _send_email(subject: str, body: str):
    try:
        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = NOTIFY_FROM
        msg["To"] = NOTIFY_TO

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(NOTIFY_FROM, [NOTIFY_TO], msg.as_string())
        logger.info(f"Daily digest email sent to {NOTIFY_TO}")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")


def _send_slack(top: list, scan_date: str):
    try:
        import urllib.request

        blocks = []
        for s in top:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{s.symbol}* — {s.short_name}\nValue: {s.value_score} | Quality: {s.quality_score} | ${s.price:.2f}"
                }
            })

        payload = json.dumps({
            "text": f"52W Low Scanner — {len(top)} top picks · {scan_date}",
            "blocks": blocks[:10],
        }).encode()

        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)
        logger.info("Daily digest sent to Slack")
    except Exception as e:
        logger.error(f"Failed to send Slack notification: {e}")
