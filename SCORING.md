# Scoring Methodology

## How Stocks Are Scored

Every stock hitting a 52-week low is evaluated through two independent lenses:

- **Value Score (0-150)** — Is this stock cheap relative to its sector peers?
- **Quality Score (0-100)** — Is this a fundamentally strong business temporarily on sale?

Both scores are computed from live Yahoo Finance data, compared against sector benchmarks from 55 blue-chip stocks (JPM, AAPL, PG, XOM, etc.) and industry peers from the scan.

---

## Value Score: The Hybrid Ratio System

The Value Score uses a **hybrid approach: 80% sector-relative ratios + 20% absolute-value sanity check**.

### How the ratio works

Each valuation metric is compared to the sector peer average:

```
ratio = stock's metric / peer average
```

For "lower is better" metrics (P/E, P/B, EV/EBITDA):

| Ratio vs Peers | Points Awarded | Interpretation |
|---------------|----------------|----------------|
| 0.30x or less | 100% of max | Extremely cheap vs peers |
| 0.31x - 0.50x | 85% of max | Very cheap vs peers |
| 0.51x - 0.65x | 65% of max | Cheap vs peers |
| 0.66x - 0.80x | 45% of max | Below peers |
| 0.81x - 0.95x | 20% of max | Slightly below peers |
| 0.96x+ | 0 pts | In line or above — no bonus, no penalty |

For "higher is better" metrics (ROE, Dividend Yield):

| Ratio vs Peers | Points Awarded | Interpretation |
|---------------|----------------|----------------|
| 2.5x+ | 100% of max | Significantly above sector |
| 1.8x - 2.5x | 80% of max | Strong vs sector |
| 1.3x - 1.8x | 60% of max | Good vs sector |
| 1.0x - 1.3x | 30% of max | Solid |
| 0.7x - 1.0x | 10% of max | Slightly below |
| Below 0.7x | 0 pts | Well below |

### The 20% absolute nudge

The ratio handles 80% of the score. The remaining 20% is an absolute-value check that catches edge cases:

- A stock at 0.5x sector P/E gets ratio points, but if the actual P/E is 8, it gets a **bonus** (P/E 8 is cheap in any context)
- A stock at 0.5x sector P/E gets ratio points, but if the actual P/E is 45, there's **no bonus** (the sector is just expensive)
- A stock with P/B below 1.0 (trading below book value) always gets a bonus regardless of sector comparison

**Absolute thresholds:**

| Metric | Strong | Cheap | Expensive |
|--------|--------|-------|-----------|
| Forward P/E | < 8 | < 12 | > 40 |
| Price/Book | < 1.0 | < 1.5 | > 8 |
| EV/EBITDA | < 6 | < 9 | > 30 |
| ROE | > 20% | > 12% | < 0% |
| Dividend Yield | > 6% | > 4% | < 0% |

### Peer average priority

When computing the ratio, the scorer picks the tightest available peer group:

1. **Industry average** (leave-one-out, 3+ peers required) — e.g., "Packaged Foods" average
2. **Market sector average** (from blue-chip benchmarks) — e.g., Consumer Defensive sector average from PG, KO, PEP, WMT, COST
3. **Scan sector average** (from other 52W-low stocks) — fallback, biased cheap

---

## Sector-Specific Scoring Models

Each sector uses different metrics with different weights, reflecting how Wall Street actually values companies in that sector. Metrics that are misleading for a sector are excluded entirely.

### Financial Services (Banks, Insurance, Asset Management)

| Metric | Max Points | Direction | Why |
|--------|-----------|-----------|-----|
| **Price/Book** | 30 | Lower is better | Book value is the primary anchor for banks |
| **ROE** | 25 | Higher is better | Profitability on equity is critical for financials |
| **ROA** | 15 | Higher is better | Asset efficiency matters for banks (scored separately) |
| **Forward P/E** | 15 | Lower is better | Earnings-based valuation as secondary check |

**Excluded:** EV/EBITDA (meaningless for financials), FCF (not how banks generate value)

### Real Estate (REITs)

| Metric | Max Points | Direction | Why |
|--------|-----------|-----------|-----|
| **Dividend Yield** | 30 | Higher is better | REITs are income vehicles — yield is primary |
| **Price/Book (NAV proxy)** | 25 | Lower is better | P/B approximates discount to Net Asset Value |
| **EV/EBITDA** | 15 | Lower is better | Adds back depreciation, more useful than P/E |
| **ROE** | 10 | Higher is better | Secondary profitability check |
| **Forward P/E** | 10 | Lower is better | Low weight, never penalized (GAAP depreciation distorts) |

**Excluded:** P/E penalties (depreciation makes P/E misleading), FCF (REITs have structural capex)

### Energy (Oil, Gas, Coal)

| Metric | Max Points | Direction | Why |
|--------|-----------|-----------|-----|
| **EV/EBITDA** | 28 | Lower is better | Cash flow based, handles capital structure differences |
| **Price/Cash Flow** | 25 | Lower is better | Computed as market cap / operating cashflow |
| **Forward P/E** | 12 | Lower is better | Secondary, less reliable for cyclical earnings |
| **Dividend Yield** | 10 | Higher is better | Energy companies are dividend payers |
| **ROE** | 8 | Higher is better | Profitability check |

**Excluded:** P/B (asset writedowns/impairments make book value unreliable)

### Healthcare (Pharma, Medtech, Services)

| Metric | Max Points | Direction | Why |
|--------|-----------|-----------|-----|
| **EV/EBITDA** | 25 | Lower is better | Primary valuation metric for healthcare |
| **Forward P/E** | 22 | Lower is better | Forward earnings capture pipeline value |
| **ROE** | 12 | Higher is better | Profitability |
| **P/B** | 8 | Lower is better | Low weight — IP/patents not captured in book |

**Extra:** Debt/equity penalty (high leverage = pipeline risk)

### Consumer Defensive (Staples)

| Metric | Max Points | Direction | Why |
|--------|-----------|-----------|-----|
| **EV/EBITDA** | 28 | Lower is better | Primary for stable-margin businesses |
| **Forward P/E** | 22 | Lower is better | Predictable earnings make P/E reliable |
| **Dividend Yield** | 12 | Higher is better | Staples are dividend aristocrats |
| **ROE** | 10 | Higher is better | Brand strength shows in ROE |

**Excluded:** P/B (brand value not captured in book value)

### Consumer Cyclical (Autos, Retail, Homebuilders)

| Metric | Max Points | Direction | Why |
|--------|-----------|-----------|-----|
| **Forward P/E (normalized)** | 28 | Lower is better | Cyclicals look expensive at trough earnings — forward P/E captures recovery |
| **Price/Book** | 20 | Lower is better | Important for asset-heavy cyclicals (homebuilders, autos) |
| **EV/EBITDA** | 18 | Lower is better | Cash flow valuation |
| **ROE** | 10 | Higher is better | Profitability through the cycle |

### Industrials (Aerospace, Machinery, Airlines, Waste)

| Metric | Max Points | Direction | Why |
|--------|-----------|-----------|-----|
| **EV/EBITDA** | 25 | Lower is better | Primary for capex-intensive businesses |
| **Forward P/E** | 22 | Lower is better | Earnings visibility is good for industrials |
| **ROE** | 12 | Higher is better | Capital efficiency |
| **P/B** | 6 | Lower is better | Low weight — intangibles dominate |

**Extra:** Debt/equity penalty (capital-intensive businesses with high debt = risk)

### Communication Services (Media, Telecom, Internet)

| Metric | Max Points | Direction | Why |
|--------|-----------|-----------|-----|
| **EV/EBITDA** | 30 | Lower is better | Primary — captures subscriber/content value |
| **Forward P/E** | 18 | Lower is better | Earnings-based secondary check |
| **ROE** | 12 | Higher is better | Profitability |
| **Dividend Yield** | 5 | Higher is better | Low weight, only for traditional telecoms |

**Excluded:** P/B (brand and IP not in book value)
**Extra:** China ADR penalty (-20 pts for stocks domiciled in China/Hong Kong)

### Basic Materials (Chemicals, Metals, Lumber)

| Metric | Max Points | Direction | Why |
|--------|-----------|-----------|-----|
| **EV/EBITDA** | 28 | Lower is better | Normalized for commodity price swings |
| **Forward P/E** | 14 | Lower is better | Less reliable due to commodity cycles |
| **Price/Book** | 12 | Lower is better | Asset value more meaningful for materials |
| **ROE** | 10 | Higher is better | Efficiency through cycles |

### Utilities (Electric, Gas, Water)

| Metric | Max Points | Direction | Why |
|--------|-----------|-----------|-----|
| **Forward P/E** | 30 | Lower is better | Utilities are bond-like — P/E is the primary metric |
| **Dividend Yield** | 25 | Higher is better | Utilities exist to pay dividends |
| **Price/Book** | 15 | Lower is better | Regulated asset base makes book value meaningful |
| **EV/EBITDA** | 12 | Lower is better | Secondary check |
| **ROE** | 8 | Higher is better | Regulated returns |

**Excluded:** FCF (utilities are chronically FCF-negative due to regulated capex), debt penalties (regulated utilities have stable, predictable EBITDA that supports higher leverage)

### Default (Technology and Unmatched Sectors)

| Metric | Max Points | Direction | Why |
|--------|-----------|-----------|-----|
| **Forward P/E** | 28 | Lower is better | Primary for growth/tech |
| **Price/Book** | 18 | Lower is better | General asset value |
| **EV/EBITDA** | 18 | Lower is better | Cash flow based |
| **ROE** | 14 | Higher is better | Profitability |

---

## Universal Signals (Applied to All Sectors)

After sector-specific valuation scoring, these signals are added to every stock:

### Positive signals

| Signal | Max Points | How |
|--------|-----------|-----|
| **FCF Yield** | +18 | FCF / Market Cap. >12% = max, >7% = solid, >3% = positive |
| **Piotroski F-Score (8-9/9)** | +10 | Composite of 9 fundamental quality tests |
| **Strong Insider Buying** | +10 | 3+ insider purchases vs sells in last 6 months |
| **Proximity to 52W Low** | +8 | Bottom 5% of range = max, bottom 20% = +3 |
| **Earnings Growing** | +5 | YoY earnings growth > 15% |
| **Revenue Growing** | +3 | YoY revenue growth > 10% |
| **Low Short Interest** | +3 | Short % of float < 3% |
| **Analyst: Strong Buy** | +10 | Consensus recommendation < 1.8 |
| **Target Upside** | +6 | Capped at 6 pts (analyst targets lag at 52W lows) |

### Negative signals (value trap detection)

| Signal | Penalty | How |
|--------|---------|-----|
| **Earnings Collapsing (>70%)** | -20 | YoY earnings decline > 70% |
| **Earnings Plunging (>50%)** | -15 | YoY earnings decline 50-70% |
| **Extreme Insider Selling (50+)** | -25 | 50+ insider sells, near-zero buys |
| **Mass Insider Exodus (20+)** | -20 | 20+ insider sells |
| **Very High Short Interest (>40%)** | -15 | Unless insiders are buying (squeeze signal) |
| **Very Weak Piotroski (0-1/9)** | -15 | Fundamental quality is terrible |
| **Leverage 5x+ Sector Avg** | -15 | Debt/Equity far above peers |
| **Extreme Cash Burn (>50% of mkt cap)** | -15 | Burning cash faster than market cap |
| **Negative EBITDA** | -12 | Operating losses |
| **Negative Forward Earnings** | -8 | No path to profitability |
| **Compounding Red Flags** | -8 to -15 | When 2+ severe penalties stack |

### Interaction effects

- **High short + insider buying** = reduced short penalty (potential squeeze)
- **Mixed insider activity** (both buying and selling) = smaller impact than pure selling
- **3+ severe red flags** = extra -15 compound penalty ("high value trap risk")

---

## Quality Score: Great Business at a Fair Price

The Quality Score ignores valuation cheapness and focuses on business quality. It surfaces companies like Home Depot, ADP, and DoorDash that score poorly on value (premium multiples) but are fundamentally excellent businesses temporarily on sale.

| Signal | Max Points | How |
|--------|-----------|-----|
| **Piotroski F-Score** | 25 | 8-9/9 = 25 pts, 7 = 20, 6 = 12, <4 = -10 |
| **ROE vs Sector** | 20 | 2x+ sector = 20 pts, 1.3x = 14, 0.8x = 8 |
| **Earnings Growth** | 15 | >25% = 15 pts, >10% = 10, declining = penalty |
| **Revenue Growth** | 10 | >15% = 10 pts, >5% = 6, declining = penalty |
| **Insider Confidence** | 10 | 2+ insider buys = 10 pts, 10+ sells = -12 |
| **Price Drop from High** | 10 | Down >40% = 10 pts, >25% = 6 (bigger opportunity) |
| **Gross Margin Trend** | 8 | Expanding margins = +8, contracting = -5 |
| **FCF Yield** | 8 | >8% = 8 pts, >3% = 4, burning cash = -5 |
| **Buyback Yield** | 5 | Active buybacks = +5, dilution = -3 |
| **Low Short Interest** | 5 | <3% = +5, >25% = -5 |
| **Also Cheap on P/E** | 5 | Mild bonus if P/E is <0.5x sector (nice-to-have) |

**Tiers:** Quality Buy (65+) | Quality Watch (45-64) | Not Quality (<45)

---

## Data Sources

- **Stock data:** Yahoo Finance quoteSummary API (defaultKeyStatistics, financialData, summaryDetail, assetProfile, insiderTransactions, incomeStatementHistory, price modules)
- **Market benchmarks:** 55 blue-chip stocks across 11 sectors, fetched fresh each scan
- **Industry averages:** Leave-one-out averages from 52W-low scan peers (3+ required)
- **Piotroski F-Score:** Computed from income statement history + current financialData
- **Insider transactions:** Last 6 months of insider buys and sells
- **Short interest:** From defaultKeyStatistics.shortPercentOfFloat

---

## Score Interpretation

| Value Score | Tier | Meaning |
|------------|------|---------|
| 70+ | Strong Value | Fundamentally cheap with confirming signals |
| 45-69 | Moderate Value | Somewhat cheap, mixed signals |
| 0-44 | Limited Signal | Not convincingly cheap, or red flags present |

| Quality Score | Tier | Meaning |
|------------|------|---------|
| 65+ | Quality Buy | Excellent business temporarily on sale |
| 45-64 | Quality Watch | Good business, worth monitoring |
| 0-44 | Not Quality | Not a quality compounder, or deteriorating |

The best opportunities are stocks that rank highly on **both** scores — cheap AND high quality. These are rare but represent the highest-conviction picks.
