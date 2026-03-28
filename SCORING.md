# Scoring Methodology

## Migration Notes — 2026-03-28

**15 new scoring signals added:**
1. **GP/A (Novy-Marx)** — Quality Score: Gross Profit / Total Assets, +12 max. Skip financial/REIT.
2. **ROIC vs WACC** — Quality Score: ROIC spread vs sector WACC (Damodaran), +8/-6. Skip financial/REIT.
3. **P/FFO for REITs** — Replaces EV/EBITDA in REIT sector model. 15 pts max.
4. **Beneish M-Score** — Value Score: Earnings manipulation flag, -10/-5. ACCT badge (amber). Skip financial/REIT.
5. **Asset growth penalty** — Value Score: >30% growth = -5, shrinkage = +3. FCF modifier. Skip REIT.
6. **Default/Tech model update** — P/B reduced 18→8 pts. EV/Gross Profit added (10 pts).
7. **Institutional ownership** — Quality Score: <15% = +4 (undiscovered), >80% = -3 (crowded).
8. **Shareholder yield** — Value Score: Dividend + buyback combined, +6 max.
9. **Enhanced buyback at 52W low** — Quality Score: +5→+8 when near bottom of range.
10. **Debt maturity risk** — Value Score: Current LTD / Total LTD, -8/-4. Compound with IC.
11. **Graham NCAV net-net** — Value Score: Price vs NCAV/share, +15/+8/+3. F-Score cross-check.
12. **Biotech detection** — Healthcare: Cash runway replaces standard metrics. BIOT badge (teal).
13. **Market cap badges** — MICRO (<$50M, gray), SMALL ($50-150M, gray).

**New badges:** ACCT (amber, M-Score > -1.78), BIOT (teal, pre-revenue biotech), MICRO/SMALL (gray, market cap), EARN (gold, earnings within 10 days).

### 2026-03-28 (Priority fixes)

1. **Double-counting fix** — Revenue growth, earnings growth, relative momentum, FCF yield, analyst upside, and revenue acceleration were scored at full weight in BOTH Value and Quality scores. Quality Score contributions now halved to prevent 2x inflation. Each signal has a documenting comment.
2. **Composite Score** — New field: `composite_score = (value_score/150 * 50) + (quality_score/100 * 50)`, normalizing both to 0-100. Used for V+Q tier ranking.
3. **Named constants** — All magic number thresholds extracted: `VALUE_STRONG_THRESHOLD`, `VQ_VALUE_THRESHOLD`, `QUALITY_BUY_THRESHOLD`, etc.
4. **BIOT scoring updated** — Cash runway: >8Q = +20, 5-8Q = +12, 3-5Q = +5, <3Q = -15.
5. **MICRO parity** — Quality Score analyst upside now applies same 50% micro-cap downweight as Value Score.
6. **EARN badge** — Gold badge when earnings within 10 calendar days. Informational, no score impact.
7. **Days-to-cover** — Short interest enhanced: days_to_cover > 10 adds +3 pts ("squeeze fuel").
8. **Score stability edge case** — !! alert no longer fires on Day 1 (no baseline).
9. **Asset growth split** — Distinguishes goodwill-driven growth (-8 penalty) from organic growth (-5). Goodwill fetched from balance sheet.

**Data layer:** yfinance balance sheet now fetched (Total Assets, PPE, Receivables, LTD, Current Debt, Shares Outstanding). WACC estimates added to Damodaran benchmarks.

---

## Overview

Every stock hitting a 52-week low is evaluated through two independent lenses:

- **Value Score (0-150)** — Is this stock cheap relative to its sector peers?
- **Quality Score (0-100)** — Is this a fundamentally strong business temporarily on sale?

Both scores use live data from Yahoo Finance quoteSummary API + yfinance financials, compared against blended sector benchmarks (60% live blue-chip data from 55 stocks + 40% Damodaran NYU Stern sector medians) and leave-one-out industry peer averages.

**Badges:**
- **V+Q** (purple) — Strong Value AND Quality Buy. Highest conviction.
- **RISK** (red) — Distress flag triggered (Z-Score, interest coverage, payout ratio, or leverage).
- **NEW** (blue) — Stock appeared since the previous scan.
- **!!** (orange delta) — Score changed ≥15 pts from previous scan.

---

## Value Score: Hybrid Ratio System

**80% sector-relative ratios + 20% absolute-value sanity check.**

### Ratio component (80%)

```
ratio = stock's metric / peer average
```

For "lower is better" metrics (P/E, P/B, EV/EBITDA):

| Ratio vs Peers | Points | Interpretation |
|---------------|--------|----------------|
| ≤ 0.30x | 100% of max | Extremely cheap vs peers |
| 0.31x - 0.50x | 85% of max | Very cheap vs peers |
| 0.51x - 0.65x | 65% of max | Cheap vs peers |
| 0.66x - 0.80x | 45% of max | Below peers |
| 0.81x - 0.95x | 20% of max | Slightly below peers |
| 0.96x+ | 0 pts | In line or above — no penalty |

For "higher is better" metrics (ROE, Dividend Yield):

| Ratio vs Peers | Points | Interpretation |
|---------------|--------|----------------|
| ≥ 2.5x | 100% of max | Significantly above sector |
| 1.8x - 2.5x | 80% of max | Strong vs sector |
| 1.3x - 1.8x | 60% of max | Good vs sector |
| 1.0x - 1.3x | 30% of max | Solid |
| 0.7x - 1.0x | 10% of max | Slightly below |
| < 0.7x | 0 pts | Well below |

### Absolute component (20%)

Catches cases where the ratio is misleading (e.g., P/E 40 at 0.5x a P/E 80 sector):

| Metric | Strong bonus | Cheap bonus | Expensive penalty |
|--------|-------------|-------------|-------------------|
| Forward P/E | < 8 | < 12 | > 40 |
| Price/Book | < 1.0 | < 1.5 | > 8 |
| EV/EBITDA | < 6 | < 9 | > 30 |
| ROE | > 20% | > 12% | < 0% |
| Dividend Yield | > 6% | > 4% | < 0% |

### Peer average priority

1. **Industry average** (leave-one-out, 3+ peers required) — e.g., "Packaged Foods" avg excluding the stock itself
2. **Market sector average** (blended: 60% live blue-chip benchmarks + 40% Damodaran medians)
3. **Scan sector average** (from other 52W-low stocks — fallback, biased cheap)

### Sector benchmark blending

Live blue-chip benchmarks (55 stocks like JPM, AAPL, PG, XOM) are blended with Damodaran NYU Stern January 2026 sector medians. This provides a more representative "normal" valuation than blue-chips alone (which skew premium) or the scan stocks (which skew cheap).

Togglable via `USE_DAMODARAN_BLEND` env var or the Settings panel.

---

## Sector-Specific Scoring Models

Each sector uses different metrics with different weights. Metrics misleading for a sector are excluded entirely.

| Sector | Primary Metrics (max pts) | Excluded | Extra |
|--------|--------------------------|----------|-------|
| **Financial** | P/B (30), ROE (25), ROA (15), Fwd P/E (15) | EV/EBITDA, FCF | — |
| **REIT** | Div Yield (30), P/B-NAV (25), **P/FFO (15)**, ROE (10), P/E (10) | P/E penalties, FCF, EV/EBITDA | Div sustainability |
| **Energy** | EV/EBITDA (28), P/CF (25), Fwd P/E (12), Div Yield (10), ROE (8) | P/B | — |
| **Healthcare** | EV/EBITDA (25), Fwd P/E (22), ROE (12), P/B (8) | — | Debt penalty |
| **Staples** | EV/EBITDA (28), Fwd P/E (22), Div Yield (12), ROE (10) | P/B | Div sustainability |
| **Cyclical** | Fwd P/E (28), P/B (20), EV/EBITDA (18), ROE (10) | — | — |
| **Industrial** | EV/EBITDA (25), Fwd P/E (22), ROE (12), P/B (6) | — | Debt penalty |
| **Comms** | EV/EBITDA (30), Fwd P/E (18), ROE (12), Div Yield (5) | P/B | China ADR penalty, Div sustainability |
| **Materials** | EV/EBITDA (28), Fwd P/E (14), P/B (12), ROE (10) | — | — |
| **Utilities** | Fwd P/E (30), Div Yield (25), P/B (15), EV/EBITDA (12), ROE (8) | FCF, Debt | Div sustainability |
| **Default/Tech** | Fwd P/E (28), P/B (**8**), EV/EBITDA (18), ROE (14), **EV/GP (10)** | — | — |

---

## Universal Signals — Value Score

Applied to every stock after sector-specific scoring:

### FCF Yield on Enterprise Value

FCF / Enterprise Value (per Alpha Architect 40-year study — EV-based FCF yield is top-2 most predictive metric).

| FCF/EV Yield | Points | |
|-------------|--------|--|
| > 12% | +18 | Strong FCF/EV yield |
| > 7% | +14 | Solid FCF/EV yield |
| > 3% | +8 | Positive FCF |
| > 0% | +4 | Positive |
| < -2% | -8 | Negative FCF yield |

**Sector-relative bonus:** +4 if FCF yield > 1.5x sector average.

### Analyst Target Upside

| Upside | Points | |
|--------|--------|--|
| > 60% | +10 | |
| > 40% | +8 | |
| > 25% | +5 | |
| > 10% | +3 | |
| < 0% | -5 | Target below current price |

### Growth Direction

**Earnings** (with EBITDA cross-check — if GAAP earnings collapse but EBITDA is stable, penalty reduced 50%):

| Earnings Growth | Points | |
|----------------|--------|--|
| < -70% | -20 | Collapsing (halved if EBITDA stable) |
| < -50% | -15 | Plunging (reduced 30% if revenue growing) |
| < -30% | -10 | Declining |
| < -10% | -5 | Declining |
| > 15% | +5 | Growing |

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

**Revenue acceleration:** +4 if current YoY growth exceeds prior YoY growth by 5%+.

### Relative Momentum

Stock's 12-month drop vs sector average drop:

| Relative Performance | Points | |
|---------------------|--------|--|
| +15pp or better | +8 | Strong positive relative momentum |
| +5pp to +15pp | +5 | Outperforming sector |
| 0 to +5pp | +3 | Slightly ahead |
| -8pp to -20pp | -2 | |
| < -20pp | -5 | Underperforming sector peers badly |

### Proximity to 52W Low — Interaction Only

No standalone bonus. Only fires as interaction with Piotroski F-Score:

| Condition | Points | |
|-----------|--------|--|
| Bottom 5% AND F-Score ≥ 7 | +6 | Potential capitulation bottom |
| Bottom 5% AND F-Score ≤ 3 | -5 | Possible free fall |
| Bottom 20% AND F-Score ≥ 7 AND FCF yield > 5% | +4 | Recovery with quality + FCF |

### Insider Transactions

Buy signals unchanged. Sell penalties reduced ~40% (most sells are pre-planned 10b5-1):

| Signal | Points | |
|--------|--------|--|
| Strong buying (3+ buys, >70% buy ratio) | +10 | |
| Insider buying (2+ buys) | +6 | |
| Net buying | +3 | |
| Selling (5-19 sells) | -5 | |
| Mass selling (20-49 sells) | -10 | |
| Extreme selling (50+ sells) | -15 | |

### Piotroski F-Score

Conservative scoring — raw pass count, no inflation for partial data:

| F-Score | Points | |
|---------|--------|--|
| 8-9/9 | +10 | Strong fundamentals |
| 6-7/9 | +5 | Good fundamentals |
| 2-3/9 | -10 | Value trap risk |
| 0-1/9 | -15 | Very weak |

### Net Debt/EBITDA (replaces Debt/Equity)

Skipped for utilities and REITs (structural leverage):

| Net Debt/EBITDA | Points | |
|----------------|--------|--|
| < -0.5 (large net cash) | +8 | Fortress balance sheet |
| < 0 (net cash) | +5 | |
| 0 to 2.5x | 0 | Healthy |
| 2.5x to 4x | -3 | Elevated |
| 4x to 6x | -10 | High leverage |
| > 6x | -15 | Extreme leverage |

### Altman Z-Score

Bankruptcy risk predictor. Skipped for financial, REIT, utility:

| Z-Score | Points | |
|---------|--------|--|
| < 1.81 | -20 | Distress zone |
| 1.81 - 2.99 | -8 | Grey zone |
| > 2.99 | 0 | Safe |
| > 4.0 | +5 | Very safe |

### Interest Coverage (EBIT / Interest Expense)

Skipped for financial and REIT:

| Coverage | Points | |
|----------|--------|--|
| < 1.0x | -18 | EBIT doesn't cover interest |
| 1.0x - 1.5x | -10 | One bad quarter from distress |
| 1.5x - 2.5x | -4 | Below average |
| 2.5x - 5.0x | 0 | Acceptable |
| 5.0x - 10x | +4 | Strong |
| > 10x | +8 | Excellent debt service capacity |

**Compound:** If Net Debt/EBITDA > 4x AND coverage < 2x → additional -10.

### Dividend Sustainability (Payout Ratio)

Only for REIT, utility, staples, comms:

| Payout Ratio | Points | |
|-------------|--------|--|
| > 150% | -15 | Unsustainable |
| 100% - 150% | -8 | At risk of cut |
| 85% - 100% | -3 | Stretched |
| 40% - 85% | 0 | Sustainable |
| < 40% | +4 | Conservative |

**Override:** Penalty halved if FCF covers the dividend even when GAAP earnings don't.

### Accruals Quality (Sloan 1996)

`(Net Income - Operating Cashflow) / Total Assets`. Skipped for financial/REIT:

| Accruals Ratio | Points | |
|---------------|--------|--|
| < -10% | +8 | High earnings quality — cash well above reported |
| < -5% | +5 | Good earnings quality |
| < 0% | +2 | Slight positive quality |
| 0% to 5% | 0 | Neutral |
| 5% to 10% | -5 | Moderate accruals concern |
| 10% to 20% | -10 | Earnings quality concern |
| > 20% | -15 | Very high accruals risk |

### Historical Mean Reversion

Compares the stock to its own history. Cap: +12 pts combined.

**Dividend yield vs 5-year average** (doubled for utilities, skipped for energy):

| Current / 5Y Avg Yield | Points | |
|------------------------|--------|--|
| > 1.5x | +8 | Historically cheap |
| > 1.25x | +5 | Above 5-year average |
| > 1.0x | +2 | At or above average |
| < 0.75x | -5 | Below historical average |

**Forward P/E recovery ratio** (fwd/trailing PE, skipped for financial/REIT/energy):

| Fwd / Trailing PE | Points | |
|-------------------|--------|--|
| < 0.60 | +8 | Earnings recovery priced in |
| < 0.75 | +5 | Improving earnings expected |
| < 0.85 | +2 | Slight improvement expected |
| > 1.20 | -5 | Earnings expected to decline |
| > 1.50 | -10 | Significant deterioration |

### Other Universal Signals

| Signal | Points |
|--------|--------|
| Low short interest (< 3%) | +3 |
| Elevated short (> 15%) | -3 |
| High short (> 25%) | -8 (reduced if insiders buying) |
| Very high short (> 40%) | -15 (reduced to -5 if insiders buying) |
| Missing forward P/E | -4 (skip for financial/REIT) |
| Negative forward earnings | -8 (skip for financial/REIT) |
| Negative EBITDA | -12 |
| 2+ severe red flags | -8 (compounding) |
| 3+ severe red flags | -15 (high value trap risk) |

### Beneish M-Score (Earnings Manipulation)

8-variable model detecting likely earnings manipulation. Skipped for financial/REIT.

| M-Score | Points | |
|---------|--------|--|
| > -1.78 | -10 | Likely manipulator (ACCT badge) |
| > -2.22 | -5 | Elevated risk |
| ≤ -2.22 | 0 | Normal |

### Asset Growth Penalty

YoY total asset growth. Skipped for REIT.

| Growth | Points | |
|--------|--------|--|
| > 30% | -5 | Potential overinvestment (halved if FCF yield > 5%) |
| > 20% | -3 | High growth (halved if FCF yield > 5%) |
| < -10% | +3 | Restructuring value |

### Shareholder Yield (Dividend + Buyback)

| Yield | Points | |
|-------|--------|--|
| > 8% | +6 | |
| > 5% | +4 | |
| > 3% | +2 | |

### Debt Maturity Risk

Current portion of LTD / Total LTD. Skipped for utility/REIT.

| Maturity Ratio | Points | |
|---------------|--------|--|
| > 50% | -8 | High near-term maturity |
| > 30% | -4 | Elevated maturity |
| + IC < 2x | -5 additional | Compound refinancing risk |

### Graham NCAV (Net-Net)

Price / NCAV per share. Skipped for financial/REIT.

| Ratio | Points | |
|-------|--------|--|
| < 1.0x | +15 | True net-net |
| < 1.5x | +8 | Near net-net |
| < 2.0x | +3 | Close to NCAV |
| + F-Score ≥ 7 | +5 additional | Quality cross-check |
| + F-Score ≤ 3 | 50% reduction | Weak fundamentals |

### China ADR Discount

Configurable via Settings panel or `CHINA_ADR_PENALTY` env var. Default: -20 pts. Range: -25 to 0.

---

## Quality Score: Great Business at a Fair Price

Ignores valuation cheapness. Focuses on business quality and whether the price drop is an opportunity.

| Signal | Max Points | How |
|--------|-----------|-----|
| **Piotroski F-Score** | 25 | 8-9 = 25, 7 = 20, 6 = 12, <4 = -10 |
| **ROIC** | 10 | >25% = 10, >15% = 7, >8% = 4, <0% = -6. Skip financial/REIT |
| **ROE vs Sector** | 15 | 2x+ = 15, 1.3x = 10, 0.8x = 5 |
| **Earnings Growth** | 15 | >25% = 15, >10% = 10, <-40% = -15 |
| **Revenue Growth** | 10 | >15% = 10, >8% = 7, >3% = 4, >0% = 2, <-15% = -15 |
| **Gross Margin Trend** | 14 | >2pp expansion = 14, >0.5pp = 8, stable = 4, <-2pp = -10, <-4pp = -15 |
| **EPS Revision Momentum** | 8+4+5 | Surprise trend improving = +8, consistent beats = +4, contrarian setup = +5 |
| **FCF Yield on EV** | 8 | >8% = 8, >3% = 4, <-5% = -5 |
| **Insider Confidence** | 10 | 2+ buys = 10, 10+ sells = -8, 5+ sells = -4 |
| **Price Drop from High** | 10 | Down >40% = 10, >25% = 6 |
| **Accruals Quality** | 6 | Cash > reported = +6, high accruals = -8 to -12 |
| **Relative Momentum** | 8 | Outperforming sector = +8 to +3, underperforming = -5 |
| **Low Short Interest** | 5 | <3% = +5, >25% = -5 |
| **Buyback Yield** | 5 | Active buybacks = +5, dilution = -3 |
| **Analyst Upside** | 10 | >60% = +10, <0% = -5 |
| **Also Cheap on P/E** | 5 | P/E < 0.5x sector |
| **Revenue Acceleration** | 4 | Current growth > prior by 5%+ |
| **Gross Profitability (GP/A)** | 12 | >40% = 12, >25% = 8, >15% = 4. Skip financial/REIT |
| **ROIC vs WACC** | 8 | >2x WACC = 8, >1.5x = 6, >WACC = 3, below = -2, negative = -6 |
| **Institutional Ownership** | 4 | <15% = +4 (undiscovered), >80% = -3 (crowded) |
| **Buyback at 52W Low** | 8 | Active buybacks near bottom = +8 (was +5) |

**Tiers:** Quality Buy (65+) | Quality Watch (45-64) | Not Quality (<45)

---

## Key Signal: Contrarian Setup

The highest-conviction signal. When ALL of these are true:
- Stock is in the bottom 10% of its 52-week range
- EPS surprise trend is positive (analyst estimates improving)

Triggers a gold-highlighted callout: **"Improving estimates at 52-week low — high-conviction contrarian setup"** (+5 bonus in Quality Score).

---

## Risk Flags

Red "RISK" badge shown in table. Detail panel shows color-coded explanation cards:

| Risk Flag | Threshold | Severity | Detail Panel Explanation |
|-----------|-----------|----------|------------------------|
| Altman Z-Score | < 1.81 | High | Distress zone — bankruptcy risk |
| Altman Z-Score | < 2.99 | Medium | Grey zone — moderate stress |
| Interest Coverage | < 1.0x | High | EBIT doesn't cover interest |
| Interest Coverage | < 1.5x | Medium | Thin coverage |
| Payout Ratio | > 150% | High | Dividend unsustainable |
| Payout Ratio | > 100% | Medium | Payout exceeds earnings |
| Net Debt/EBITDA | > 6x | High | Extreme leverage |
| Net Debt/EBITDA | > 4x | Medium | High leverage |
| Beneish M-Score | > -1.78 | High (ACCT badge) | Possible earnings manipulation |
| Beneish M-Score | > -2.22 | Medium | Elevated manipulation risk |
| Debt Maturity | > 50% | High | Near-term refinancing risk |

### Additional Badges

| Badge | Color | Trigger | Meaning |
|-------|-------|---------|---------|
| **BIOT** | Teal | Healthcare + negative EBITDA + low revenue + no dividend | Pre-revenue biotech — cash runway scored instead of standard metrics |
| **MICRO** | Gray | Market cap < $50M | Micro-cap: analyst upside downweighted 50%, low liquidity warning |
| **SMALL** | Gray | Market cap $50-150M | Small-cap indicator, no score impact |

---

## Earnings Quality Indicator

Shown in detail panel based on Accruals Quality Ratio:

| Accruals Ratio | Label | Color |
|---------------|-------|-------|
| < -5% | High | Green |
| -5% to +5% | Normal | Gray |
| +5% to +15% | Watch | Amber |
| > +15% | Risk | Red |

---

## Score Stability

To reduce noise from single-day data fluctuations:

- **5-day rolling average** computed for both Value and Quality scores
- **Days in scan** counter tracks how many consecutive days a stock has appeared
- **Large change alert** (!! indicator) when score changes ≥15 pts from previous day

---

## Data Sources

| Source | What it provides |
|--------|-----------------|
| Yahoo quoteSummary API | defaultKeyStatistics, financialData, summaryDetail, assetProfile, insiderTransactions, incomeStatementHistory, price, earningsHistory |
| yfinance `ticker.financials` | Complete income statement: EBIT, EBITDA, Interest Expense, Gross Profit (data the API lacks) |
| 55 blue-chip benchmarks | Live market-level sector averages (JPM, AAPL, PG, XOM, CAT, etc.) |
| Damodaran NYU Stern | January 2026 sector medians — blended 60/40 with blue-chip data |
| Industry peer averages | Leave-one-out averages from scan peers (3+ required) |

---

## Scheduled Jobs

| Job | Schedule | What it does |
|-----|----------|-------------|
| Full scan | 4:30 PM ET daily | Fetch all 52W-low stocks, enrich, score, save |
| Pre-market refresh | 7:00 AM ET Mon-Fri | Update prices only, flag stocks exiting 52W low range |
| Notifications | After full scan | Email/Slack digest of top 10 new/improved picks |
| Performance tracking | After full scan | Save price + scores for 30/90/180 day return validation |

---

## Configuration

| Setting | Default | Env Var | Adjustable at Runtime |
|---------|---------|---------|----------------------|
| China ADR penalty | -20 | `CHINA_ADR_PENALTY` | Yes (Settings panel) |
| Damodaran blend | On | `USE_DAMODARAN_BLEND` | Yes (Settings panel) |
| Notifications | Off | `NOTIFY_ENABLED` | Yes |
| CORS origins | localhost | `CORS_ORIGINS` | No (restart required) |
| Settings API key | None | `SETTINGS_API_KEY` | No |
| SMTP config | None | `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS` | No |
| Slack webhook | None | `SLACK_WEBHOOK_URL` | No |

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

The best opportunities are stocks that rank highly on **both** scores — cheap AND high quality. These get a purple **V+Q** badge and represent the highest-conviction picks in the system.
