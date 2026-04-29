"""Bulk-add current holdings to the Portfolio tab via the FastAPI server.

Usage:
    python3 scripts/add_portfolio.py                  # default: prod (scanner.sasfaw.com)
    python3 scripts/add_portfolio.py --target local   # localhost:8000
    python3 scripts/add_portfolio.py --url https://staging.example.com

Idempotent: any symbol the server already has (HTTP 409) is logged as
"skipped" rather than failing. Edit the POSITIONS list or CASH below to
update what gets posted.

Cash is stored as a synthetic row with symbol='CASH', shares=$amount,
cost_basis=1.0 — the Portfolio API treats this as the cash line for
NAV/weight math without needing a separate table.

Initial seed dated 2026-04-29 — Vanguard general portfolio review.
Cost basis figures are average-per-share.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_TARGETS = {
    "prod": "https://scanner.sasfaw.com/api/portfolio",
    "local": "http://localhost:8000/api/portfolio",
}

# Active positions — (symbol, short_name, shares, avg cost basis $/share).
POSITIONS = [
    ("AMR",  "Alpha Metallurgical Resources",   200,   226.00),
    ("CNR",  "Core Natural Resources",          265,    98.33),
    ("ULTA", "Ulta Beauty",                      40,   402.00),
    ("INFQ", "Infleqtion (quantum)",          1_000,    13.70),
    ("JD",   "JD.com",                          350,    32.46),
    ("PYPL", "PayPal Holdings",                 150,    77.52),
    ("STLA", "Stellantis N.V.",                 694,    16.99),
    ("IWM",  "iShares Russell 2000 ETF",         21,   186.63),
    ("OXY",  "Occidental Petroleum",            105,    56.33),
    ("NKE",  "Nike, Inc.",                      104,    82.90),
    ("PAYC", "Paycom Software",                  30,   159.76),
    ("GPI",  "Group 1 Automotive",               10,   379.00),
    ("LAD",  "Lithia Motors",                    12,   268.58),
    ("SIRI", "Sirius XM Holdings",              106,    31.07),
    ("BAC",  "Bank of America",                  33,    24.82),
    ("WFC",  "Wells Fargo & Co.",                 4,    41.90),
]

# Cash position — stored as symbol='CASH' so the same CRUD path serves
# positions and cash. shares = dollars; cost_basis = 1.0.
CASH = {
    "amount": 87_000.00,
    "notes": (
        "Idle cash earmarked for redeployment per April 2026 portfolio review. "
        "Park in SGOV / T-bills (~4.5% yield) until trim plan executes and Tier 1 "
        "buys (ELV, BSX, PEP, PODD) are sized."
    ),
}


def _post(api_url: str, body: dict) -> tuple[str, str]:
    """POST a single portfolio row. Returns (status, message).

    status is one of "added" / "exists" / "failed". Treats HTTP 409 as
    a benign skip so the script is idempotent across reruns.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        api_url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return "added", f"{body['symbol']}: HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return "exists", f"{body['symbol']}: already in portfolio"
        snippet = e.read().decode("utf-8", errors="replace")[:200]
        return "failed", f"{body['symbol']}: HTTP {e.code} — {snippet}"
    except Exception as e:
        return "failed", f"{body['symbol']}: {type(e).__name__}: {e}"


def build_items() -> list[dict]:
    """Build the list of POST bodies (positions + cash row)."""
    items: list[dict] = []
    for sym, name, shares, cost in POSITIONS:
        items.append({
            "symbol": sym,
            "short_name": name,
            "shares": float(shares),
            "cost_basis": float(cost),
            "notes": "",
            "snapshot": {"seeded": "2026-04-29"},
        })
    items.append({
        "symbol": "CASH",
        "short_name": "Cash & Equivalents",
        "shares": float(CASH["amount"]),
        "cost_basis": 1.0,
        "notes": CASH["notes"],
        "snapshot": {"seeded": "2026-04-29"},
    })
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--target", choices=DEFAULT_TARGETS.keys(), default="prod",
        help="prod (scanner.sasfaw.com) or local (localhost:8000); default prod",
    )
    parser.add_argument(
        "--url", default=None,
        help="explicit API URL override (e.g. https://staging.example.com/api/portfolio)",
    )
    args = parser.parse_args()
    api_url = args.url or DEFAULT_TARGETS[args.target]

    items = build_items()
    print(f"Posting {len(items)} portfolio rows to {api_url}\n")

    counts = {"added": 0, "exists": 0, "failed": 0}
    icons = {"added": "✅", "exists": "⏭️ ", "failed": "❌"}
    failed_symbols: list[str] = []

    for body in items:
        status, msg = _post(api_url, body)
        counts[status] += 1
        print(f"{icons[status]} {msg}")
        if status == "failed":
            failed_symbols.append(body["symbol"])

    print(
        f"\nDone. {counts['added']} added, {counts['exists']} already existed, "
        f"{counts['failed']} failed."
    )
    if failed_symbols:
        print(f"Failures: {failed_symbols}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
