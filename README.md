# 52W Low Value Scanner

A full-stack stock screener that fetches live 52-week low stocks from Yahoo Finance, enriches them with fundamental valuation metrics, compares against sector averages, and assigns a composite value score to surface fundamentally undervalued stocks.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server (starts on http://localhost:8000)
python -m server.main
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

## How It Works

1. **Fetches** the 250 most recent 52-week low stocks from Yahoo Finance
2. **Filters** out penny stocks (<$10), small caps (<$2B), and software/IT stocks
3. **Enriches** each stock with fundamentals (P/E, P/B, EV/EBITDA, ROE, FCF, analyst targets)
4. **Computes sector averages** for peer comparison
5. **Scores** each stock on a 0–100+ composite value scale
6. **Displays** results in an interactive sortable/filterable table

## Daily Schedule

A full refresh runs automatically at **4:30 PM ET** daily (after market close). You can also trigger a manual refresh via the UI button or `POST /api/scan/refresh`.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/scan` | Latest scored stock list |
| `GET` | `/api/scan/history` | Available scan dates |
| `GET` | `/api/scan/:date` | Scan for a specific date |
| `POST` | `/api/scan/refresh` | Trigger manual refresh |
| `GET` | `/api/spark/:symbol` | 1-year price history |

## Features

- **Value Scoring**: Composite score based on Forward P/E, P/B, EV/EBITDA, ROE, FCF, analyst consensus, upside potential, and debt levels
- **Sector Comparison**: Metrics color-coded green/red vs sector averages
- **Watchlist**: Star/bookmark stocks, persisted in browser localStorage
- **CSV Export**: Download filtered results
- **Historical Scans**: SQLite-backed daily history with date picker
- **Sparkline Charts**: 12-month price chart in detail panel

## Project Structure

```
ValueInvest52Lows/
├── server/
│   ├── main.py           # FastAPI app, routes, scheduler
│   ├── config.py         # Constants and configuration
│   ├── models.py         # Pydantic data models
│   ├── database.py       # SQLite persistence
│   ├── yahoo_client.py   # Yahoo Finance API client
│   ├── pipeline.py       # 6-step data pipeline
│   └── scorer.py         # Value scoring engine
├── client/
│   └── index.html        # Single-page frontend
├── data/                  # SQLite DB (auto-created)
├── requirements.txt
└── README.md
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8000` | Server port |
| `DB_PATH` | `data/scanner.db` | SQLite database path |
