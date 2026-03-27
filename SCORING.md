# Scoring Methodology

## How Stocks Are Scored

Every stock hitting a 52-week low is evaluated through two independent lenses:

- **Value Score (0-150)** — Is this stock cheap relative to its sector peers?
- **Quality Score (0-100)** — Is this a fundamentally strong business temporarily on sale?

Both scores use live data from Yahoo Finance quoteSummary API + yfinance financials, compared against 55 blue-chip sector benchmarks and industry peers.

Stocks that score highly on **both** get a purple "V+Q" badge — the highest-conviction picks.

---

## Value Score: The Hybrid Ratio System

The Value Score uses a **hybrid approach: 80% sector-relative ratios + 20% absolute-value sanity check**.

### How the ratio works

```
ratio = stock's metric / peer average
```

For "lower is better" metrics (P/E, P/B, EV/EBITDA):

| Ratio vs Peers | Points | Interpretation |
|---------------|--------|----------------|
| 0.30x or less | 100% of max | Extremely cheap vs peers |
| 0.31x - 0.50x | 85% of max | Very cheap vs peers |
| 0.51x - 0.65x | 65% of max | Cheap vs peers |
| 0.66x - 0.80x | 45% of max | Below peers |
| 0.81x - 0.95x | 20% of max | Slightly below peers |
| 0.96x+ | 0 pts | In line or above — no penalty |

For "higher is better" metrics (ROE, Dividend Yield):

| Ratio vs Peers | Points | Interpretation |
|---------------|--------|----------------|
| 2.5x+ | 100% of max | Significantly above sector |
| 1.8x - 2.5x | 80% of max | Strong vs sector |
| 1.3x - 1.8x | 60% of max | Good vs sector |
| 1.0x - 1.3x | 30% of max | Solid |
| 0.7x - 1.0x | 10% of max | Slightly below |
| Below 0.7x | 0 pts | Well below |

### The 20% absolute nudge

Catches edge cases where the ratio is misleading:

| Metric | Strong bonus | Cheap bonus | Expensive penalty |
|--------|-------------|-------------|-------------------|
| Forward P/E | < 8 | < 12 | > 40 |
| Price/Book | < 1.0 | < 1.5 | > 8 |
| EV/EBITDA | < 6 | < 9 | > 30 |
| ROE | > 20% | > 12% | < 0% |
| Dividend Yield | > 6% | > 4% | < 0% |

### Peer average priority

1. **Industry average** (leave-one-out, 3+ peers required)
2. **Market sector average** (55 blue-chip benchmarks, fetched fresh each scan)
3. **Scan sector average** (other 52W-low stocks — fallback)

---

## Sector-Specific Scoring Models

Each sector uses different metrics with different weights. Metrics misleading for a sector are excluded.

| Sector | Primary Metrics (max pts) | Excluded | Extra |
|--------|--------------------------|----------|-------|
| **Financial** | P/B (30), ROE (25), ROA (15), Fwd P/E (15) | EV/EBITDA, FCF | — |
| **REIT** | Div Yield (30), P/B-NAV (25), EV/EBITDA (15), ROE (10), P/E (10) | P/E penalties, FCF | Div sustainability check |
| **Energy** | EV/EBITDA (28), P/CF (25), Fwd P/E (12), Div Yield (10), ROE (8) | P/B | — |
| **Healthcare** | EV/EBITDA (25), Fwd P/E (22), ROE (12), P/B (8) | — | Debt penalty |
| **Staples** | EV/EBITDA (28), Fwd P/E (22), Div Yield (12), ROE (10) | P/B | Div sustainability check |
| **Cyclical** | Fwd P/E (28), P/B (20), EV/EBITDA (18), ROE (10) | — | — |
| **Industrial** | EV/EBITDA (25), Fwd P/E (22), ROE (12), P/B (6) | — | Debt penalty |
| **Comms** | EV/EBITDA (30), Fwd P/E (18), ROE (12), Div Yield (5) | P/B | China ADR -20, Div check |
| **Materials** | EV/EBITDA (28), Fwd P/E (14), P/B (12), ROE (10) | — | — |
| **Utilities** | Fwd P/E (30), Div Yield (25), P/B (15), EV/EBITDA (12), ROE (8) | FCF, Debt | Div sustainability check |
| **Default/Tech** | Fwd P/E (28), P/B (18), EV/EBITDA (18), ROE (14) | — | — |

---

## Universal Signals — Value Score

Applied after sector-specific scoring to every stock:

### FCF Yield on Enterprise Value (C1)

FCF / Enterprise Value (not market cap — per Alpha Architect 40-year study).

| FCF/EV Yield | Points | |
|-------------|--------|--|
| > 12% | +18 | Strong FCF yield |
| > 7% | +14 | Solid FCF yield |
| > 3% | +8 | Positive FCF |
| > 0% | +4 | Positive |
| < -2% | -8 | Negative FCF yield |

**Bonus:** +4 if FCF yield > 1.5x sector average.

### Analyst Target Upside (C7)

| Upside | Points | |
|--------|--------|--|
| > 60% | +10 | |
| > 40% | +8 | |
| > 25% | +5 | |
| > 10% | +3 | |
| < 0% (below price) | -5 | Target below current price |

### Growth Direction (C6, C8)

**Earnings** (with EBITDA cross-check):

| Earnings Growth | Points | |
|----------------|--------|--|
| Collapsing (< -70%) | -20 | Halved if EBITDA stable (GAAP one-time items) |
| Plunging (< -50%) | -15 | Reduced 30% if revenue growing (margin compression) |
| Declining (< -30%) | -10 | |
| Declining (< -10%) | -5 | |
| Growing (> 15%) | +5 | |

**Revenue** (enhanced tiers):

| Revenue Growth | Points | |
|---------------|--------|--|
| > 15% | +5 | Strong |
| > 8% | +4 | Solid |
| > 3% | +3 | Positive |
| > 0% | +2 | Modest but positive |
| 0 to -5% | -3 | |
| -5% to -15% | -8 | Declining |
| < -15% | -15 | Collapsing |

**Revenue acceleration:** +4 if current YoY growth > prior YoY growth by 5%+

### Relative Momentum (C5)

Stock's 12-month drop vs sector average drop.

| Relative Performance | Points | |
|---------------------|--------|--|
| +15pp or better | +8 | Strong positive relative momentum |
| +5pp to +15pp | +5 | Outperforming sector peers |
| 0 to +5pp | +3 | Slightly ahead |
| -8pp to -20pp | -2 | |
| < -20pp | -5 | Underperforming sector peers badly |

### Proximity to 52W Low — Interaction Only (C12)

No standalone bonus. Only fires as interaction with Piotroski F-Score:

| Condition | Points | |
|-----------|--------|--|
| Bottom 5% of range AND F-Score >= 7 | +6 | Potential capitulation bottom |
| Bottom 5% of range AND F-Score <= 3 | -5 | Possible free fall |
| Bottom 20% AND F-Score >= 7 AND FCF yield > 5% | +4 | Recovery + quality + FCF |

### Insider Transactions (C2)

Buy signals unchanged. Sell penalties reduced ~40% (most sells are pre-planned 10b5-1).

| Signal | Points | |
|--------|--------|--|
| Strong buying (3+ buys, >70% buy ratio) | +10 | |
| Insider buying (2+ buys) | +6 | |
| Net buying | +3 | |
| Selling (5-19 sells) | -5 | Reduced from -12 |
| Mass selling (20-49 sells) | -10 | Reduced from -20 |
| Extreme selling (50+ sells) | -15 | Reduced from -25 |

### Piotroski F-Score

| F-Score | Points | |
|---------|--------|--|
| 8-9/9 | +10 | Strong fundamentals |
| 6-7/9 | +5 | Good fundamentals |
| 2-3/9 | -10 | Value trap risk |
| 0-1/9 | -15 | Very weak |

### Leverage: Net Debt/EBITDA (C10)

Replaces the old Debt/Equity metric. Skipped for utilities and REITs.

| Net Debt/EBITDA | Points | |
|----------------|--------|--|
| < -0.5 (large net cash) | +8 | Fortress balance sheet |
| < 0 (net cash) | +5 | |
| 0 to 2.5x | 0 | Healthy |
| 2.5x to 4x | -3 | Elevated |
| 4x to 6x | -10 | High leverage |
| > 6x | -15 | Extreme leverage |

### Altman Z-Score (C9)

Bankruptcy risk predictor. Skipped for financial, REIT, utility sectors.

| Z-Score | Points | |
|---------|--------|--|
| < 1.81 | -20 | Distress zone — bankruptcy risk |
| 1.81 - 2.99 | -8 | Grey zone — elevated risk |
| > 2.99 | 0 | Safe |
| > 4.0 | +5 | Very safe |

### Interest Coverage (C13)

EBIT / Interest Expense. Skipped for financial and REIT sectors.

| Coverage | Points | |
|----------|--------|--|
| < 1.0x | -18 | EBIT doesn't cover interest |
| 1.0x - 1.5x | -10 | One bad quarter from distress |
| 1.5x - 2.5x | -4 | Below average |
| 2.5x - 5.0x | 0 | Acceptable |
| 5.0x - 10x | +4 | Strong |
| > 10x | +8 | Excellent debt service capacity |

**Compound:** If Net Debt/EBITDA > 4x AND coverage < 2x → additional -10.

### Dividend Sustainability (C11)

Payout ratio check. Only for REIT, utility, staples, comms sectors.

| Payout Ratio | Points | |
|-------------|--------|--|
| > 150% | -15 | Dividend likely unsustainable |
| 100% - 150% | -8 | At risk of cut |
| 85% - 100% | -3 | Stretched |
| 40% - 85% | 0 | Sustainable |
| < 40% | +4 | Conservative, well covered |

**Override:** Penalty halved if FCF covers the dividend even when GAAP earnings don't.

### Accruals Quality (Change A)

Sloan (1996) accrual anomaly: `(Net Income - Operating Cashflow) / Total Assets`. Skipped for financial/REIT.

| Accruals Ratio | Points | |
|---------------|--------|--|
| < -10% | +8 | High earnings quality — cash well above reported |
| < -5% | +5 | Good earnings quality |
| < 0% | +2 | Slight positive quality |
| 0% to 5% | 0 | Neutral |
| 5% to 10% | -5 | Moderate accruals concern |
| 10% to 20% | -10 | High accruals — earnings quality concern |
| > 20% | -15 | Very high accruals risk |

### Historical Mean Reversion (Change C)

Compares the stock to its own historical valuation. Cap: +12 pts combined.

**Dividend yield vs 5-year average** (doubled for utilities, skipped for energy):

| Current / 5Y Avg Yield | Points | |
|------------------------|--------|--|
| > 1.5x | +8 | Historically cheap — yielding 50%+ above average |
| > 1.25x | +5 | Above 5-year average |
| > 1.0x | +2 | At or above average |
| < 0.75x | -5 | Below historical average |

**Forward P/E recovery ratio** (forward PE / trailing PE, skipped for financial/REIT/energy):

| Fwd / Trailing PE | Points | |
|-------------------|--------|--|
| < 0.60 | +8 | Earnings recovery priced in |
| < 0.75 | +5 | Improving earnings expected |
| < 0.85 | +2 | Slight improvement expected |
| > 1.20 | -5 | Earnings expected to decline |
| > 1.50 | -10 | Significant deterioration expected |

### Other Universal Signals

| Signal | Points | |
|--------|--------|--|
| Low short interest (< 3%) | +3 | |
| Elevated short (> 15%) | -3 | |
| High short (> 25%) | -8 | Reduced if insiders buying |
| Very high short (> 40%) | -15 | Reduced to -5 if insiders buying |
| Missing forward P/E | -4 | Skipped for financial/REIT |
| Negative forward earnings | -8 | Skipped for financial/REIT |
| Negative EBITDA | -12 | Operating losses |
| 2+ severe red flags | -8 | Compounding |
| 3+ severe red flags | -15 | High value trap risk |

---

## Quality Score: Great Business at a Fair Price

Ignores valuation cheapness. Focuses on business quality and whether the price drop is an opportunity.

| Signal | Max Points | How |
|--------|-----------|-----|
| **Piotroski F-Score** | 25 | 8-9/9 = 25, 7 = 20, 6 = 12, <4 = -10 |
| **ROIC** (C4) | 10 | >25% = 10 (exceptional), >15% = 7, >8% = 4, <0% = -6. Skip financial/REIT |
| **ROE vs Sector** | 15 | 2x+ sector = 15, 1.3x = 10, 0.8x = 5 |
| **Earnings Growth** | 15 | >25% = 15, >10% = 10, <-40% = -15 |
| **Revenue Growth** (C6) | 10 | >15% = 10, >8% = 7, >3% = 4, >0% = 2, <-15% = -15 |
| **Gross Margin Trend** (C3) | 14 | >100bps expansion = 14, >50bps = 10, stable = 4, <-200bps = -10, <-400bps = -15 |
| **FCF Yield on EV** | 8 | >8% = 8, >3% = 4, <-5% = -5 |
| **Insider Confidence** | 10 | 2+ buys = 10, 10+ sells = -8, 5+ sells = -4 |
| **Price Drop from High** | 10 | Down >40% = 10, >25% = 6 |
| **EPS Revision Momentum** (Change B) | 8+4+5 | Surprise trend improving = +8, consistent beats = +4, contrarian setup = +5 |
| **Accruals Quality** (Change A) | 6 | Cash > reported = +6, high accruals = -8 to -12 |
| **Relative Momentum** (C5) | 8 | Outperforming sector = +8 to +3, underperforming = -5 |
| **Low Short Interest** | 5 | <3% = +5, >25% = -5 |
| **Buyback Yield** | 5 | Active buybacks = +5, dilution = -3 |
| **Analyst Upside** (C7) | 10 | >60% = +10, <0% = -5 |
| **Also Cheap on P/E** | 5 | P/E < 0.5x sector |
| **Revenue Acceleration** (C6) | 4 | Current growth > prior growth by 5%+ |

**Tiers:** Quality Buy (65+) | Quality Watch (45-64) | Not Quality (<45)

---

## Contrarian Setup Signal (Change B)

The highest-conviction signal in the system. When ALL of these are true:
- Stock is in the bottom 10% of its 52-week range
- EPS surprise trend is positive (estimates improving)

This triggers a gold-highlighted callout in the detail panel: **"Improving estimates at 52-week low — high-conviction contrarian setup"** (+5 bonus pts in Quality Score).

---

## Risk Flags

Stocks with any of these get a red "RISK" badge in the table:

| Risk Flag | Threshold | Severity |
|-----------|-----------|----------|
| Altman Z-Score | < 1.81 | High (distress zone) |
| Altman Z-Score | < 2.99 | Medium (grey zone) |
| Interest Coverage | < 1.0x | High (can't cover interest) |
| Interest Coverage | < 1.5x | Medium (thin coverage) |
| Payout Ratio | > 120% | Medium |
| Payout Ratio | > 150% | High (unsustainable) |
| Net Debt/EBITDA | > 4x | Medium |
| Net Debt/EBITDA | > 6x | High (extreme leverage) |

The detail panel shows each risk flag with a color-coded card explaining the metric value and what it means in plain English.

---

## Earnings Quality Indicator

Shown in the detail panel based on the Accruals Quality Ratio:

| Accruals Ratio | Label | Color |
|---------------|-------|-------|
| < -5% | High | Green |
| -5% to +5% | Normal | Gray |
| +5% to +15% | Watch | Amber |
| > +15% | Risk | Red |

---

## Data Sources

| Source | What it provides |
|--------|-----------------|
| Yahoo quoteSummary API | defaultKeyStatistics, financialData, summaryDetail, assetProfile, insiderTransactions, incomeStatementHistory, price, earningsHistory |
| yfinance `ticker.financials` | Complete income statement (EBIT, EBITDA, Interest Expense, Gross Profit — data the API lacks) |
| 55 blue-chip benchmarks | Market-level sector averages (JPM, AAPL, PG, XOM, CAT, etc.) |
| Industry peer averages | Leave-one-out averages from scan peers (3+ required) |

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

| Badge | Meaning |
|-------|---------|
| **V+Q** (purple) | Strong Value AND Quality Buy — highest conviction |
| **RISK** (red) | Distress flag triggered (Z-Score, coverage, payout, leverage) |
| **NEW** (blue) | Stock appeared since the previous scan |
