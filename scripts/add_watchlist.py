"""Bulk-add Potential Buys to the watchlist via the FastAPI server.

Usage:
    python3 scripts/add_watchlist.py                  # default: prod (scanner.sasfaw.com)
    python3 scripts/add_watchlist.py --target local   # localhost:8000
    python3 scripts/add_watchlist.py --url https://staging.example.com

Idempotent: any symbol the server already has (HTTP 409) is logged as
"skipped" rather than failing. Edit the ITEMS list below to add or
modify what gets posted.

Initial seed dated 2026-04-29 — Tier 1 high-conviction value/quality
adds from a portfolio review session, plus a watchlist of pullback
candidates. Each item carries a thesis, target price/event/date, yes/no
due-diligence questions mapped from the kill switches, free-form notes
including sizing guidance, and a JSON snapshot of screen scores.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_TARGETS = {
    "prod": "https://scanner.sasfaw.com/api/watchlist",
    "local": "http://localhost:8000/api/watchlist",
}

ITEMS = [
    # ===== TIER 1 — High Conviction =====
    {
        "symbol": "ELV",
        "short_name": "Elevance Health, Inc.",
        "thesis": (
            "Cleanest deep-value setup in healthcare. Fwd P/E 11.8x vs UNH 19x. P/B 1.73x "
            "(sector 4.09x). 4.9% combined shareholder yield. Q1 2026 (Apr 21) confirmed "
            "the inflection — guidance raised from $25.50 to $26.75. Management calls 2026 "
            "the 'trough year' for Medicaid (-1.75% op margin) and MA repositioning. 2027 "
            "target: 12%+ adjusted EPS growth. Carelon Services +47% Q4 YoY = real growth "
            "engine. Anthem BCBS license in 14 states is irreplaceable moat. 45.2M members. "
            "ROIC 16.5% vs WACC 8.2%. $6.7B buyback authorized = ~9% of float. "
            "Tier 1 — highest conviction value pick at 5-6% target weight."
        ),
        "target_price": 344.0,
        "target_event": "Q2 2026 earnings (~late July 2026) — confirm MBR stabilization",
        "target_date": "2026-07-25",
        "questions": [
            {"question": "MBR stays below 91% in 2026 quarters (no further deterioration)?", "answer": "", "confirmed": False},
            {"question": "Carelon Services growth holds above 25%?", "answer": "", "confirmed": False},
            {"question": "2027 EPS growth guidance comes in at 8%+?", "answer": "", "confirmed": False},
            {"question": "No major regulatory shock (MFA committee passage, large PBM legislation)?", "answer": "", "confirmed": False},
            {"question": "No heavy insider selling (3+ executives in one quarter)?", "answer": "", "confirmed": False},
            {"question": "Stock holds above $273 (52W low)?", "answer": "", "confirmed": False},
            {"question": "Medicaid op margin improves toward 0% by 2H 2026 (off the -1.75% trough)?", "answer": "", "confirmed": False},
            {"question": "MA op margin holds at 2%+ in 2026?", "answer": "", "confirmed": False},
        ],
        "notes": (
            "TIER 1 — 5-6% sizing (~$13-15k). Entry plan: 60% now @ $344, 20% at $320, "
            "20% at $300. KILL SWITCHES (exit immediately if): MBR >91% any quarter, "
            "Carelon Services <25% growth, 2027 EPS guide <8%, MFA/PBM legislation passes, "
            "3+ executives sell heavily, stock breaks $273 (trim 50%). "
            "Risk class: large-cap healthcare quality at deep value multiple."
        ),
        "snapshot": {"price_at_add": 344.76, "value_score": 83, "quality_score": 37, "f_score": 7, "tier": 1, "added_date": "2026-04-29"},
    },
    {
        "symbol": "BSX",
        "short_name": "Boston Scientific Corporation",
        "thesis": (
            "Cleanest med device exposure with no recall overhang. Fwd P/E 15x, $3.24B FCF, "
            "F-Score 8/9, 62% analyst upside, 0B/5S insider activity (heavy buying). "
            "Farapulse PFA + Watchman LAA = two best-in-class procedure-volume franchises. "
            "Diversified portfolio (cardiology, MedSurg, peripheral) = no single-product "
            "recall risk like PODD. Heavy M&A roll-up (Farapulse, Silk Road, Acotec) is the "
            "tradeoff — goodwill-heavy balance sheet. Tier 1 — 4% target weight as quality "
            "med device anchor."
        ),
        "target_price": 60.0,
        "target_event": "Q2 2026 earnings — Farapulse share data vs J&J Varipulse / Medtronic PulseSelect ramp",
        "target_date": "2026-07-30",
        "questions": [
            {"question": "Farapulse share holds above 50% as J&J Varipulse + Medtronic PulseSelect launch?", "answer": "", "confirmed": False},
            {"question": "Watchman growth stays positive (>5% YoY)?", "answer": "", "confirmed": False},
            {"question": "No major M&A goodwill writedown (>$500M impairment)?", "answer": "", "confirmed": False},
            {"question": "Operating margin holds above 25%?", "answer": "", "confirmed": False},
            {"question": "Hospital capex environment supports procedure volumes (>5% growth)?", "answer": "", "confirmed": False},
            {"question": "Insider net buying continues (positive B/S ratio)?", "answer": "", "confirmed": False},
        ],
        "notes": (
            "TIER 1 — 4% sizing (~$10k). Entry: 60% now @ $59-60, 25% at $55, 15% at $50. "
            "KILL SWITCHES: Farapulse share <50% post-competitor ramp, Watchman growth turns "
            "negative, M&A writedown >$500M, op margin <25%, hospital capex slowdown >5% impact. "
            "Risk class: large-cap quality compounder, premium multiple but durable moat."
        ),
        "snapshot": {"price_at_add": 59.52, "value_score": 80, "quality_score": 81, "f_score": 8, "tier": 1, "added_date": "2026-04-29"},
    },
    {
        "symbol": "PEP",
        "short_name": "PepsiCo, Inc.",
        "thesis": (
            "Defensive cash machine at 4-year multiple low. Fwd P/E 18.1x vs 5-yr avg 22-25x. "
            "FCF expanding ~40% to ~$11B in 2026 as TCJA payment expires + capex drops below "
            "5% of revenue. 54-year dividend aristocrat at 3.57% yield. Elliott Management "
            "activist stake = governance catalyst (potential beverage spin-off, margin reset). "
            "GLP-1 is the real risk — Frito-Lay NA volumes have been negative 6+ quarters. "
            "Reformulation push around Lay's, Tostitos, Gatorade, Quaker targets the GLP-1 "
            "consumer. Tier 1 — 3.5% target as staples ballast."
        ),
        "target_price": 157.0,
        "target_event": "Q2 2026 earnings — Frito-Lay NA volume inflection",
        "target_date": "2026-07-15",
        "questions": [
            {"question": "Frito-Lay NA organic volume turns positive by end of FY26?", "answer": "", "confirmed": False},
            {"question": "Elliott activist stake remains and pushes structural changes?", "answer": "", "confirmed": False},
            {"question": "FCF guidance holds at $9.5B+ for 2026?", "answer": "", "confirmed": False},
            {"question": "Dividend growth streak intact (no cut or pause)?", "answer": "", "confirmed": False},
            {"question": "No major capital allocation misstep (no overpriced M&A >$5B)?", "answer": "", "confirmed": False},
            {"question": "Reformulation initiatives showing volume traction in Lay's/Tostitos/Gatorade?", "answer": "", "confirmed": False},
        ],
        "notes": (
            "TIER 1 — 3.5% sizing (~$9k). Entry: 50% now @ $157, 30% at $148, 20% at $140. "
            "KILL SWITCHES: FLNA volume doesn't inflect by end FY26, Elliott exits without "
            "forcing change, FCF guide cut <$9.5B, dividend cut/pause, M&A misstep >$5B. "
            "Risk class: large-cap staples defensive, low beta, dividend aristocrat."
        ),
        "snapshot": {"price_at_add": 157.67, "value_score": None, "quality_score": None, "f_score": None, "tier": 1, "added_date": "2026-04-29"},
    },
    {
        "symbol": "PODD",
        "short_name": "Insulet Corporation",
        "thesis": (
            "Quality Score 100/100 at 48% drawdown. 2025 revenue $2.7B (+31%), gross margin "
            "72.2% (best-in-class med device), ROIC 17.5% vs WACC 8.2%, expanding to Type 2 "
            "diabetes (4x TAM) and 25 countries (+44% intl growth). Just refinanced $420M "
            "convertibles to $450M senior notes (less dilution). $452M EOFlow trade secret "
            "judgment validates IP moat. RECALL WATCH: March 2026 voluntary correction on "
            "~1.5% of pod lots (Class I designation, 476 serious injuries, 0 deaths, manageable "
            "manufacturing defect — not design flaw). Insider buying through recall (2B/1S). "
            "Tandem Sigi tubeless pump launches late 2026/2027 = competitive risk. "
            "Tier 1 starter — 2.5-3% sizing reflects recall uncertainty."
        ),
        "target_price": 165.0,
        "target_event": "Q1 2026 earnings — recall scope, FY26 guide revision, new patient starts",
        "target_date": "2026-05-08",
        "questions": [
            {"question": "Recall scope stays below 5% of production (no expansion)?", "answer": "", "confirmed": False},
            {"question": "Zero deaths reported linked to the recall?", "answer": "", "confirmed": False},
            {"question": "No FDA Form 483 with major manufacturing observations?", "answer": "", "confirmed": False},
            {"question": "2026 revenue guidance holds above +15% growth?", "answer": "", "confirmed": False},
            {"question": "Insider activity stays net buying (or neutral) through recall period?", "answer": "", "confirmed": False},
            {"question": "Tandem Sigi launch delayed beyond Q3 2026?", "answer": "", "confirmed": False},
            {"question": "Q1 2026 new patient starts decline less than 30% YoY?", "answer": "", "confirmed": False},
            {"question": "Omnipod 6 (2027) approval timeline holds (no delay beyond Q3 2027)?", "answer": "", "confirmed": False},
            {"question": "Adverse event count stays below 1,000 serious injuries?", "answer": "", "confirmed": False},
            {"question": "Type 2 diabetes adoption tracking >15% of new starts within 12 months?", "answer": "", "confirmed": False},
        ],
        "notes": (
            "TIER 1 (recall watch) — START SMALL 2.5-3% (~$6-8k). Entry: 50% now @ $183, "
            "25% post-Q1 reaction (~$165-170), 25% post-Q2 once recall scope is fully known. "
            "KILL SWITCHES: recall expands >5% of production, ANY death linked to recall, "
            "FDA Form 483 major observations, 2026 guide cut <+15%, insiders flip to selling, "
            "Tandem Sigi launches before Q3 2026, new starts drop >30%, Omnipod 6 delayed past "
            "Q3 2027, AE count >1,000 serious injuries. "
            "Risk class: quality growth at GARP multiple with active recall risk."
        ),
        "snapshot": {"price_at_add": 182.87, "value_score": 79, "quality_score": 100, "f_score": 7, "tier": 1, "added_date": "2026-04-29", "active_recall": True},
    },
    # ===== TIER 2 — Build/Add =====
    {
        "symbol": "WFC",
        "short_name": "Wells Fargo & Company",
        "thesis": (
            "Build out existing stub position (currently 0.2%). ROTCE reached 15.0% in 2025, "
            "management raised target to 17-18%. $18B buybacks executed in 2025, $23B total "
            "capital returned. Asset cap removed. Fwd P/E 13.6x. Operating leverage thesis: "
            "cost cuts + fee income growth (wealth, cards) + capital return = mid-teens "
            "annualized return potential. Tier 2 — proper 3% position vs current stub."
        ),
        "target_price": 81.55,
        "target_event": "Q3 2026 earnings — ROTCE trajectory toward 17%+ target",
        "target_date": "2026-10-15",
        "questions": [
            {"question": "Asset cap remains lifted (no Fed re-imposition)?", "answer": "", "confirmed": False},
            {"question": "ROTCE holds above 13% in consecutive quarters?", "answer": "", "confirmed": False},
            {"question": "No new regulatory consent order issued?", "answer": "", "confirmed": False},
            {"question": "CEO Charlie Scharf remains in position?", "answer": "", "confirmed": False},
            {"question": "Net charge-off ratio stays below 1.2% (credit cycle intact)?", "answer": "", "confirmed": False},
            {"question": "Buyback pace sustained ($15B+ annual run-rate)?", "answer": "", "confirmed": False},
        ],
        "notes": (
            "TIER 2 BUILD — Currently 0.2% stub, target 3% (~$7k additional). "
            "KILL SWITCHES: asset cap re-imposed, ROTCE <13% for 2 consecutive quarters, "
            "new consent order, Scharf departure, NCO ratio >1.2%. "
            "Risk class: large-cap bank, post-restructuring operating leverage."
        ),
        "snapshot": {"price_at_add": 81.55, "value_score": None, "quality_score": None, "f_score": None, "tier": 2, "added_date": "2026-04-29"},
    },
    # ===== TIER 3 — Speculative =====
    {
        "symbol": "IONQ",
        "short_name": "IonQ, Inc.",
        "thesis": (
            "Best-quality public quantum bet. 2025 revenue $130M (+202% YoY) — first quantum "
            "company over $100M GAAP revenue. $370M backlog. $3.3B cash, no debt = 10+ years "
            "runway. AQ 64 milestone reached ahead of schedule. World-record 99.99% two-qubit "
            "fidelity. Vertical integration via M&A: Oxford Ionics ($1.08B), Lightsynq, "
            "Capella (space QKD), SkyWater foundry. Customers: AWS, Azure, Google Cloud, "
            "KISTI, QuantumBasel, Hyundai, Airbus, US gov. SWAP from INFQ (sell INFQ entirely, "
            "buy IONQ). Higher quality on every metric (revenue, cash, partnerships). "
            "Tier 3 speculation — 3-3.5% max sizing as venture-style bet."
        ),
        "target_price": 35.0,
        "target_event": "Q4 2026 earnings — AQ 1024 progress, EBITDA loss trajectory",
        "target_date": "2027-02-25",
        "questions": [
            {"question": "AQ 1024 milestone tracking to end-of-2027 target?", "answer": "", "confirmed": False},
            {"question": "Cash burn stays below $400M/year?", "answer": "", "confirmed": False},
            {"question": "Stock-based dilution stays below 15% in 2026?", "answer": "", "confirmed": False},
            {"question": "No competitor (IBM, Google) demonstrates clear quantum advantage first?", "answer": "", "confirmed": False},
            {"question": "Niccolo de Masi and core technical leadership stay in place?", "answer": "", "confirmed": False},
            {"question": "Revenue growth stays above 50% YoY in 2026?", "answer": "", "confirmed": False},
            {"question": "Cash position stays above $1.5B (no urgent capital raise needed)?", "answer": "", "confirmed": False},
        ],
        "notes": (
            "TIER 3 SPECULATION — 3-3.5% max (~$8-9k). REPLACES INFQ (sell INFQ entirely). "
            "KILL SWITCHES: AQ 1024 slips beyond 2027, cash burn >$400M/yr without revenue match, "
            "dilution >15% in any year, IBM/Google demonstrates quantum advantage first, "
            "de Masi or technical leadership departure, revenue growth <50%, cash <$1.5B. "
            "Risk class: pre-profit deep tech speculation. Treat as money-you-can-lose. "
            "5-yr scenarios: Bull +100-200%, Base 0 to +50%, Bear -60 to -80%."
        ),
        "snapshot": {"price_at_add": None, "value_score": None, "quality_score": None, "f_score": None, "tier": 3, "added_date": "2026-04-29", "speculative": True},
    },
    # ===== WATCHLIST — Buy on Pullback =====
    {
        "symbol": "ABT",
        "short_name": "Abbott Laboratories",
        "thesis": (
            "Backup healthcare cash machine. $7.4B FCF, 52+ year dividend aristocrat, "
            "diversified across med devices + diagnostics + nutrition + established pharma. "
            "Fwd P/E 15x. WAIT for trigger: <$85 (fwd P/E ~14x) or PODD recall expansion "
            "creating need for clean med device exposure. Main risks: Similac NEC litigation "
            "($5-15B potential aggregate liability), FreeStyle Libre vs Dexcom G7 share war, "
            "GLP-1 impact on CGM TAM. Boring + defensive + reliable."
        ),
        "target_price": 85.0,
        "target_event": "Pullback to $85 OR PODD recall expansion forces healthcare diversification",
        "target_date": "2026-07-31",
        "questions": [
            {"question": "Stock pulls back to $85 or below (fwd P/E ~14x)?", "answer": "", "confirmed": False},
            {"question": "Similac NEC litigation aggregate exposure stays below $10B?", "answer": "", "confirmed": False},
            {"question": "FreeStyle Libre growth holds above 15% YoY?", "answer": "", "confirmed": False},
            {"question": "GLP-1 impact on CGM volumes remains modest (single-digit headwind)?", "answer": "", "confirmed": False},
            {"question": "No insider selling acceleration (B/S ratio stays neutral or positive)?", "answer": "", "confirmed": False},
        ],
        "notes": (
            "WATCHLIST — Trigger: <$85 OR PODD recall expansion. Target 3% sizing (~$7k). "
            "Currently at $92.72 (fwd P/E 15.3x). Backup/alternative to PODD if recall worsens. "
            "Risk class: large-cap healthcare quality, diversified, dividend aristocrat."
        ),
        "snapshot": {"price_at_add": 92.72, "value_score": 49, "quality_score": 33, "f_score": 5, "tier": "watchlist", "added_date": "2026-04-29", "trigger_price": 85.0},
    },
    {
        "symbol": "BMI",
        "short_name": "Badger Meter, Inc.",
        "thesis": (
            "High-quality water utility compounder. 4-year revenue CAGR 13%, EPS CAGR 28%. "
            "ROIC 28.5% vs WACC 9.9% = strong value creation. Net cash, ORION Cellular + "
            "BEACON SaaS + SmartCover sewer monitoring expansion. Sticky utility customers, "
            "secular AMI tailwind. PROBLEM: still 24x fwd P/E, 2026 organic revenue guided "
            "FLAT (AMI project timing). WAIT for trigger: <$100 (fwd P/E ~20x) OR Q3 2026 "
            "AMI award reacceleration confirmed. Insider buying suggests they think it's "
            "cheap; market disagrees on growth pause."
        ),
        "target_price": 100.0,
        "target_event": "Pullback to $100 OR Q3 2026 AMI award reacceleration",
        "target_date": "2026-10-31",
        "questions": [
            {"question": "Stock pulls back to $100 or below (fwd P/E ~20x)?", "answer": "", "confirmed": False},
            {"question": "AMI awards visibly reaccelerate in 2H 2026?", "answer": "", "confirmed": False},
            {"question": "SmartCover ARR scales toward $100M run-rate?", "answer": "", "confirmed": False},
            {"question": "Insider buying continues at $120 or below?", "answer": "", "confirmed": False},
            {"question": "Operating margin holds at 20%+ despite revenue flatness?", "answer": "", "confirmed": False},
            {"question": "No further competitive share loss to Itron in core meter business?", "answer": "", "confirmed": False},
        ],
        "notes": (
            "WATCHLIST — Trigger: <$100 OR confirmed AMI reacceleration. Target 3% sizing (~$7k). "
            "Currently at $120.89 (fwd P/E 24.6x). Quality compounder, but multiple is fair-not-cheap "
            "and 2026 organic growth is zero. Wait. "
            "Risk class: small-cap industrial quality, secular tailwind."
        ),
        "snapshot": {"price_at_add": 120.89, "value_score": 40, "quality_score": 69, "f_score": 7, "tier": "watchlist", "added_date": "2026-04-29", "trigger_price": 100.0},
    },
    {
        "symbol": "UNH",
        "short_name": "UnitedHealth Group Incorporated",
        "thesis": (
            "Highest-quality managed care operator (MBR 83.9% vs ELV 90%) but ran 49% off "
            "lows ($234 → $350) — chasing it now is the wrong setup. Q1 2026 (Apr 21) beat "
            "with EPS $7.23 vs $6.57 and raised FY26 EPS guide to >$18.25. 2027 MA rate "
            "increase of 2.48% finalized = locked-in tailwind. WAIT for pullback to <$300 "
            "(fwd P/E ~16x) before adding. Already own ELV as the deep-value managed care "
            "play. UNH is the quality version — premium price, premium operations."
        ),
        "target_price": 300.0,
        "target_event": "Pullback to <$300 (fwd P/E ~16x)",
        "target_date": "2026-09-30",
        "questions": [
            {"question": "Stock pulls back to $300 or below?", "answer": "", "confirmed": False},
            {"question": "MBR remains best-in-class (below 84%)?", "answer": "", "confirmed": False},
            {"question": "Optum Health growth stays above 15% YoY?", "answer": "", "confirmed": False},
            {"question": "DOJ probe outcome doesn't include large fines or operational restrictions?", "answer": "", "confirmed": False},
            {"question": "MA membership growth holds positive in 2026?", "answer": "", "confirmed": False},
        ],
        "notes": (
            "WATCHLIST — Trigger: <$300. Already own ELV (deep value version); UNH is the "
            "quality version at higher multiple. Don't chase the +49% rally. Target 3-4% if "
            "trigger hits. Risk class: large-cap healthcare quality, premium operator."
        ),
        "snapshot": {"price_at_add": 350.0, "value_score": None, "quality_score": None, "f_score": None, "tier": "watchlist", "added_date": "2026-04-29", "trigger_price": 300.0},
    },
    {
        "symbol": "QBTS",
        "short_name": "D-Wave Quantum Inc.",
        "thesis": (
            "Quantum annealing specialist (different exposure from gate-based IONQ). 2025 "
            "revenue $24.6M (+179% YoY), 100+ paying customers, $635M cash post Quantum "
            "Circuits acquisition. Advantage2 system claims 'quantum supremacy on a useful "
            "real-world problem.' Recent wins: €10M Italy facility, $20M Florida Atlantic "
            "purchase, $10M F100 enterprise QCaaS contract. ONLY add if pursuing a 2-name "
            "quantum basket strategy alongside IONQ — annealing is a specialized tool, not "
            "general-purpose quantum. Skip if doing single-name (IONQ) approach."
        ),
        "target_price": 8.0,
        "target_event": "Q3 2026 earnings — annealing customer pipeline, $1M+ deal count",
        "target_date": "2026-11-15",
        "questions": [
            {"question": "Confirmed pursuing 2-name quantum basket (IONQ + QBTS) vs single-name strategy?", "answer": "", "confirmed": False},
            {"question": "Annealing revenue growth stays above 100% YoY in 2026?", "answer": "", "confirmed": False},
            {"question": "Customer count crosses 150+ organizations?", "answer": "", "confirmed": False},
            {"question": "$1M+ deal pipeline keeps expanding?", "answer": "", "confirmed": False},
            {"question": "Cash position stays above $400M (no urgent capital raise)?", "answer": "", "confirmed": False},
            {"question": "No general-purpose quantum advantage achievement that obsoletes annealing?", "answer": "", "confirmed": False},
        ],
        "notes": (
            "WATCHLIST — Optional quantum basket hedge. ONLY buy if doing 2-name quantum strategy "
            "(IONQ 2.5% + QBTS 1.5%, total 4%). Skip if doing single-name IONQ approach. "
            "KILL SWITCHES: revenue growth <50%, cash <$400M, gate-based competitor achieves "
            "quantum advantage first (annealing becomes footnote). "
            "Risk class: pre-profit deep tech speculation. Architecture-hedge play."
        ),
        "snapshot": {"price_at_add": None, "value_score": None, "quality_score": None, "f_score": None, "tier": "watchlist", "added_date": "2026-04-29", "speculative": True, "optional": True},
    },
]


def post_item(api_url: str, item: dict) -> tuple[str, str]:
    """POST a single watchlist item.

    Returns (status, message) where status is "added", "exists", or "failed".
    The server returns 409 when the symbol is already present — treat that as
    a skip so the script is idempotent across re-runs.
    """
    body = json.dumps(item).encode("utf-8")
    req = urllib.request.Request(
        api_url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return "added", f"{item['symbol']}: HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return "exists", f"{item['symbol']}: already on watchlist"
        body = e.read().decode("utf-8", errors="replace")[:200]
        return "failed", f"{item['symbol']}: HTTP {e.code} — {body}"
    except Exception as e:
        return "failed", f"{item['symbol']}: {type(e).__name__}: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--target", choices=DEFAULT_TARGETS.keys(), default="prod",
        help="prod (scanner.sasfaw.com) or local (localhost:8000); default prod",
    )
    parser.add_argument(
        "--url", default=None,
        help="explicit API URL override (e.g. https://staging.example.com/api/watchlist)",
    )
    args = parser.parse_args()

    api_url = args.url or DEFAULT_TARGETS[args.target]
    print(f"Posting {len(ITEMS)} items to {api_url}\n")

    counts = {"added": 0, "exists": 0, "failed": 0}
    icons = {"added": "✅", "exists": "⏭️ ", "failed": "❌"}
    failed_symbols: list[str] = []

    for item in ITEMS:
        status, msg = post_item(api_url, item)
        counts[status] += 1
        print(f"{icons[status]} {msg}")
        if status == "failed":
            failed_symbols.append(item["symbol"])

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
