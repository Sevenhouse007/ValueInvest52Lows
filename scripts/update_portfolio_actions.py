"""Patch each portfolio position with an action tag + detailed notes.

Usage:
    python3 scripts/update_portfolio_actions.py                # default: prod
    python3 scripts/update_portfolio_actions.py --target local
    python3 scripts/update_portfolio_actions.py --url https://staging.example.com

Each entry in ACTIONS sets:
  - snapshot.action ∈ {trim, exit, hold, build, swap, review, deploy}
  - notes: full rationale + sizing + kill switches per position

The Portfolio table renders the action as a colored chip (red EXIT,
amber TRIM, green BUILD/DEPLOY, etc.) with the notes surfaced as the
hover tooltip. Run this script after any review session that updates
the action plan to refresh both the chip and the notes in one pass.

Idempotent: PATCH is non-destructive (only fields in the body get
written), so re-running just overwrites the action + notes without
clobbering shares/cost_basis/short_name. If a symbol in ACTIONS is
not present in the portfolio, it's skipped with a warning.

Initial seed dated 2026-04-29 — Vanguard general portfolio review.
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

ACTIONS = {
    "AMR": {
        "action": "trim",
        "notes": (
            "TRIM 26% → 10% (frees ~$25k for redeployment).\n\n"
            "Currently 15.9% post fresh-price update — already trimmed conceptually but "
            "still oversized for a single-commodity cyclical. Best balance sheet in US met "
            "coal (zero debt, $524M liquidity), insiders buying, but commodity exposure must "
            "be sized at venture weight, not core.\n\n"
            "KILL SWITCHES (exit if any trip):\n"
            "  • Met coal ex-China <$110/ton for 2 consecutive quarters\n"
            "  • Kingston Wildcat misses 2026 500k-ton target by >30%\n"
            "  • Company draws on ABL revolver\n"
            "  • Buyback authorization halted entirely"
        ),
    },
    "CNR": {
        "action": "exit",
        "notes": (
            "EXIT entirely (frees ~$23k).\n\n"
            "Highly correlated with AMR (US met coal). Owning both is single-bet leverage, "
            "not diversification. AMR is the higher-quality coal asset (zero debt vs CNR's "
            "$317M; pure met vs CNR's PRB melting-ice-cube; better Q4 numbers).\n\n"
            "Tax loss available — bought at $98.33, now $91.57 = ~$1.8k harvestable loss. "
            "Combined with AMR trim, drops US coal exposure from 26%+15% = 41% down to ~10%."
        ),
    },
    "ULTA": {
        "action": "hold",
        "notes": (
            "HOLD at current ~9% weight.\n\n"
            "Highest-quality business in the book. 44M loyalty members (95%+ of sales), "
            "11% exclusive products, mass+prestige+services moat. Comps decelerating from "
            "5.4% → 2.5-3.5% in FY26 but still positive; Sephora/Amazon competition real "
            "but not breaking the franchise yet. Don't add given decel; don't trim — winner "
            "you want to keep compounding.\n\n"
            "KILL SWITCHES:\n"
            "  • Full-year comps go negative (first time post-pandemic)\n"
            "  • Sephora/Amazon visibly take measurable share in Circana data\n"
            "  • Loyalty member count declines YoY\n"
            "  • Operating margin contracts below 12%\n"
            "  • Forward P/E expands above 25x without comp re-acceleration"
        ),
    },
    "INFQ": {
        "action": "swap",
        "notes": (
            "SWAP — Sell INFQ entirely, buy IONQ at 3-3.5% sizing.\n\n"
            "Why: IONQ is the higher-quality public quantum bet on every metric — $130M "
            "FY25 revenue (+202% YoY) vs INFQ pre-scale, $3.3B cash vs smaller cushion, "
            "broader commercial proof points, vertical integration via Oxford Ionics + "
            "Lightsynq + Capella + SkyWater acquisitions. Same thesis (atomic quantum "
            "advantage by 2028), better execution.\n\n"
            "Total quantum exposure should max at 4-5% of portfolio. Owning both INFQ and "
            "IONQ stacks correlated speculation, not diversification."
        ),
    },
    "JD": {
        "action": "trim",
        "notes": (
            "TRIM 6.7% → 4% (already roughly there at 4.4%).\n\n"
            "Cheap profitable Chinese e-commerce business with $24B+ cash, 4.6% retail op "
            "margin (improving from 4.0%), real capital returns ($5-7B/yr buyback+div = "
            "~15% shareholder yield). 2026 weak comp from trade-subsidy pull-forward, but "
            "food delivery cash burn declining = $2B+ EBIT tailwind.\n\n"
            "But: VIE structure + delisting risk + Taiwan tail risk = different risk class. "
            "Cap Chinese ADR exposure at 3-5% per name regardless of cheapness.\n\n"
            "KILL SWITCHES:\n"
            "  • US enacts forced delisting of Chinese ADRs\n"
            "  • Taiwan invasion or blockade\n"
            "  • JD Retail operating margin contracts below 4%\n"
            "  • Annual dividend abandoned or buyback suspended\n"
            "  • PDD overtakes JD in total retail GMV"
        ),
    },
    "PYPL": {
        "action": "exit",
        "notes": (
            "EXIT (frees ~$8k + ~$4k harvestable tax loss).\n\n"
            "Channel-check confirms what the data showed: branded checkout +1% organic = "
            "the franchise is losing share to Apple Pay, Shop Pay, Stripe Link, Amazon Pay. "
            "User's qualitative observation ('people I know avoid it') aligns with operational "
            "reality. FCF/buyback math doesn't save you when the core merchant flow is "
            "secularly weakening.\n\n"
            "Don't average down on losers when both the numbers and your ground-level "
            "observation point the same direction."
        ),
    },
    "OXY": {
        "action": "hold",
        "margin_of_safety": "high",
        "notes": (
            "HOLD at current 2.7% weight. MoS upgraded to HIGH (was medium) on oil spike + "
            "improved capital structure. Don't add at war-driven oil prices; don't trim a "
            "Buffett-anchored position with thesis confirming.\n\n"
            "WHAT CHANGED SINCE BUY ($56.33 → $60.28, +7%):\n"
            "  • Berkshire bought OxyChem for ~$9.7B (closed Jan 2, 2026) — price-discovers\n"
            "    the chemical segment; remaining E&P + 1PointFive trades at ~7-8x EV/EBITDA\n"
            "    on backed-out math, cheaper than peer pure-play E&Ps\n"
            "  • Principal debt $23B → $15B in 12 months ($5.8B paydown from OxyChem proceeds\n"
            "    + $7.5B repaid since July 2024 from divestitures + FCF)\n"
            "  • Quarterly dividend +8% to $0.26\n"
            "  • Berkshire stake 26.7% (slight trim from ~28% but still anchor position)\n"
            "  • Stratos DAC operational target: 500k metric tons this quarter\n"
            "  • CrownRock integration smoothing — Permian breakevens mid-$30s\n\n"
            "GEOPOLITICAL CONTEXT (Apr 30, 2026): WTI ~$105-108, Brent ~$115 (touched $126),\n"
            "both up ~60% since US/Israel-Iran war began Feb 28. At $100+ oil, OXY's 2026 FCF\n"
            "trajectory is $10-12B (vs $5-6B at $70 oil). EV/FCF compresses to 6-7x. Implied\n"
            "per-share fair value $80-95 at sustained $100 oil. BUT — geopolitical premium\n"
            "is volatile; Iran de-escalation = $70-80 oil fast and 15-20% give-back on the\n"
            "stock. Don't chase the war-driven move.\n\n"
            "MARGIN OF SAFETY (HIGH):\n"
            "  ✅ Asset-backed: OxyChem sale validates ~1/3 of EV; Permian reserves at low\n"
            "     breakeven justify the rest\n"
            "  ✅ Cash flow durable: at $80 oil ~$7B FCF; at $100 ~$10-12B; cushion is huge\n"
            "  ✅ Balance sheet improving fast: $15B debt heading to $13B target by EOY\n"
            "  ✅ Owner-aligned: Buffett 26.7% + $10B preferred = quasi-controlling shareholder\n"
            "  ⚠️ Volatility: 5-10% intraday on geopolitical headlines is normal\n\n"
            "CATALYST: Q1 2026 earnings May 5/6. Analyst consensus adj EPS $0.70. Watch for:\n"
            "  • FY26 guidance revision upward\n"
            "  • Buyback re-authorization (debt now near target)\n"
            "  • Stratos commissioning update\n"
            "  • Berkshire 13F (mid-May) — were they buying through the Feb dip?\n\n"
            "KILL SWITCHES (refreshed):\n"
            "  • WTI averages below $55 for 2 consecutive quarters\n"
            "  • Berkshire reduces stake below 23% (currently 26.7%)\n"
            "  • Stratos DAC misses 500k metric ton target by >50%\n"
            "  • 45Q tax credit reduced or eliminated by Trump admin/Congress\n"
            "  • Principal debt fails to reach ~$13B by end of 2026\n"
            "  • Berkshire preferred retirement schedule paused without explanation\n"
            "  • CrownRock production guidance cut by >10% (integration failure)\n"
            "  • OXY adds significant non-US debt-funded acquisition (capital-allocation\n"
            "    discipline broken)"
        ),
    },
    "IWM": {
        "action": "review",
        "notes": (
            "REVIEW — decide if this is conviction or parking lot.\n\n"
            "iShares Russell 2000 ETF up +45.79% on cost = winner that snuck up. But owning "
            "an index fund inside a stock-picker portfolio is either:\n"
            "  (a) intentional 'small-cap basket' exposure (then keep at ~3-5% as core)\n"
            "  (b) cash that should be in SGOV at 4.5% earning while waiting\n"
            "  (c) opportunity cost for a specific small-cap thesis\n\n"
            "Currently 2.4% — small enough to ignore, big enough to ask the question. "
            "Decide and act consistently with the answer."
        ),
    },
    "STLA": {
        "action": "hold",
        "notes": (
            "HOLD at 2.2% — turnaround speculation, properly sized.\n\n"
            "Down 54.7% on cost but the 10-K deep dive showed real recovery signals: H2 "
            "2025 shipments +11% YoY (NA +39%), CEO Filosa executing, €46B liquidity > "
            "market cap = no capital-raise risk, kitchen-sink €25.4B charges done. Not "
            "obviously broken — just early in a multi-year reset.\n\n"
            "Don't add more — already correctly sized. Don't capitulate at the bottom — "
            "that's the worst entry-exit point.\n\n"
            "KILL SWITCHES (exit if any trip):\n"
            "  • CEO Filosa departure\n"
            "  • 2026 industrial FCF guidance walked back\n"
            "  • Any equity capital raise\n"
            "  • Jeep or Ram volume declines in 2026\n"
            "  • Chinese EV share in Europe exceeds 15%\n\n"
            "Reconsider trim only if STLA recovers to ~$14-15 (back near cost basis) — "
            "lock in capital, accept partial recovery."
        ),
    },
    "NKE": {
        "action": "exit",
        "notes": (
            "EXIT (frees ~$5k + ~$3.8k harvestable tax loss).\n\n"
            "Down 46.5% but still trades 23x fwd P/E with eroding moat. Different from STLA: "
            "STLA has €46B liquidity floor; NKE has premium multiple + losing share to On, "
            "Hoka, New Balance simultaneously while China is -17%. Premium price + secular "
            "competitive pressure + multi-year recovery timeline = no margin of safety.\n\n"
            "Tax loss is useful — pairs with ULTA gain trims if any are made. Better risk-"
            "adjusted return available in ELV, EPD, BSX (when yellow flag clears) at lower "
            "multiples with clearer catalysts."
        ),
    },
    "PAYC": {
        "action": "exit",
        "notes": (
            "GRADUAL EXIT (frees ~$4k).\n\n"
            "AI-substitution risk flagged. SMB payroll/HR SaaS is directly in the line of "
            "fire for AI agents that automate onboarding, benefits enrollment, time tracking, "
            "payroll processing. Switching costs and integrations protect medium-term but "
            "the multi-year thesis is harder to defend than 12-18 months ago.\n\n"
            "Don't add. Treat at current weight as a gradual exit — sell on strength, not "
            "on weakness. Redeploy into AI-immune categories per the framework (healthcare "
            "delivery, physical infrastructure, banks, staples)."
        ),
    },
    "GPI": {
        "action": "hold",
        "notes": (
            "HOLD at 1.5%.\n\n"
            "Auto retail — physical inventory + dealer franchises = AI-immune. Down 7.86% "
            "on cost but stable business. Pairs with LAD as the auto-retail sleeve. Don't "
            "add (already small bet on auto cycle), don't trim (working as intended at "
            "modest weight).\n\n"
            "Auto cycle could compress further on rates or recession; or could re-accelerate "
            "if rates fall. Modest weight = no urgency to act either direction."
        ),
    },
    "LAD": {
        "action": "hold",
        "notes": (
            "HOLD at 1.5%.\n\n"
            "Lithia Motors — auto retail with strongest acquisition/scale story among "
            "dealers. Working at +8.35% on cost. Pairs with GPI. AI-immune (physical "
            "inventory + service revenue + financing).\n\n"
            "Could be sized larger but doesn't pass the strict deep-value criteria (P/E "
            "reasonable but not Graham cheap). Hold what's working at current weight."
        ),
    },
    "SIRI": {
        "action": "exit",
        "notes": (
            "EXIT (frees ~$2.8k).\n\n"
            "Sirius XM — declining sub business + Liberty Media overhang. Down 13.87% on "
            "cost. AI not the core risk here — secular cord-cutting + competition from "
            "Spotify/Apple Music/podcasts is. Not a deep-value setup; cheap for real reasons. "
            "Free up the capital for higher-conviction deployment."
        ),
    },
    "BAC": {
        "action": "hold",
        "notes": (
            "HOLD at 0.7% — winner but tiny weight.\n\n"
            "Up 113% on cost = trophy position that proved the original thesis. But it's now "
            "fully priced (P/B 1.38, fwd P/E ~12x) — don't add at these levels. The position "
            "is small enough that it's not worth fussing about; let it run. If multiple "
            "re-rates above 1.5x book or yield drops below 2%, consider trimming back to "
            "free capital."
        ),
    },
    "WFC": {
        "action": "build",
        "notes": (
            "BUILD from 0.1% stub → 3% target (~$7k more).\n\n"
            "Q1 2026 confirmed thesis (per Potential Buys watchlist green flag): EPS $1.60 "
            "(+15% YoY), ROTCE 14.5% (vs 13.6% YoY), $4B Q1 buybacks ($16B annualized), "
            "asset cap remains lifted. NIM compression caveat from analysts but kill switches "
            "all clean.\n\n"
            "Stage entry: 60% of build now, 40% on any pullback below $78."
        ),
    },
    "CASH": {
        "action": "deploy",
        "notes": (
            "DEPLOY ~$30-50k of $87k per Tier 1 plan; keep ~$40-55k as dry powder.\n\n"
            "ACTIVE BUYS (per current watchlist flags + MoS):\n"
            "  • PEP $4-5k (green-confirmed, high MoS)\n"
            "  • WFC build $7k (green-confirmed, medium MoS)\n"
            "  • EPD $10-12k (high MoS, asset-backed midstream)\n"
            "  • ELV $13k (high MoS, deep-value managed care)\n\n"
            "WAIT (do not deploy yet):\n"
            "  • PODD red flag — wait for May 6 earnings\n"
            "  • BSX yellow flag — wait for Q2 (late July 2026)\n\n"
            "RESERVE: ~$40-55k in SGOV at ~4.5% yield = pays you $200/mo to wait. Use for "
            "next drawdown (VIX > 25 / SPY -10%) or high-conviction idea at clear discount."
        ),
    },
}


def get_all(api_url: str) -> list[dict]:
    """Pull current portfolio rows so we can merge snapshot fields cleanly."""
    with urllib.request.urlopen(api_url, timeout=30) as resp:
        return json.loads(resp.read())["items"]


def patch(api_url: str, item_id: int, body: dict) -> tuple[bool, str]:
    """PATCH a single portfolio row. Non-destructive — only the fields in
    body get written, so shares / cost_basis / short_name are preserved."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{api_url}/{item_id}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, f"id={item_id}: HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        return False, f"id={item_id}: HTTP {e.code} — {body}"
    except Exception as e:
        return False, f"id={item_id}: {type(e).__name__}: {e}"


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

    print(f"Updating portfolio actions on {api_url}\n")
    items = get_all(api_url)
    by_sym = {it["symbol"]: it for it in items}

    counts = {"updated": 0, "missing": 0, "failed": 0}
    failed: list[str] = []
    for sym, plan in ACTIONS.items():
        it = by_sym.get(sym)
        if not it:
            print(f"⏭️  {sym}: not in portfolio (skipping)")
            counts["missing"] += 1
            continue
        # Merge into snapshot: action is required; margin_of_safety is
        # optional — when set on a plan entry it's stamped on the snapshot
        # so the UI can render the MoS pill on portfolio rows the same way
        # it does on watchlist cards.
        new_snap = {**(it.get("snapshot") or {}), "action": plan["action"]}
        if plan.get("margin_of_safety"):
            new_snap["margin_of_safety"] = plan["margin_of_safety"]
        body = {"snapshot": new_snap, "notes": plan["notes"]}
        ok, msg = patch(api_url, it["id"], body)
        icon = "✅" if ok else "❌"
        mos = plan.get("margin_of_safety", "")
        mos_str = f" mos={mos}" if mos else ""
        print(f"{icon} {sym:5} action={plan['action']:7}{mos_str} {msg}")
        if ok:
            counts["updated"] += 1
        else:
            counts["failed"] += 1
            failed.append(sym)

    print(
        f"\nDone. {counts['updated']} updated, {counts['missing']} missing, "
        f"{counts['failed']} failed."
    )
    if failed:
        print(f"Failures: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
