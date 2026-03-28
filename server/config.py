"""Configuration constants for the 52W Low Value Scanner."""

import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "scanner.db"))
DATA_DIR = BASE_DIR / "data"

# Yahoo Finance endpoints
YAHOO_SCREENER_URL = (
    "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved"
)
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_QUOTE_SUMMARY_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
YAHOO_SPARK_URL = "https://query1.finance.yahoo.com/v8/finance/spark"
YAHOO_HOME_URL = "https://finance.yahoo.com"

# HTTP
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Pipeline settings
SCREENER_COUNT = 250
MIN_PRICE = 10
MIN_MARKET_CAP = 2_000_000_000
MAX_CONCURRENT_REQUESTS = 5
REQUEST_DELAY_MS = 200

# Software/IT stocks to always exclude
EXCLUDED_SYMBOLS = {
    "SAP", "ADBE", "INFY", "CTSH", "FICO", "OTEX", "APPF",
    "CVLT", "SSNC", "CHKP", "DOX", "GEN", "DBX", "NSIT",
}
EXCLUDED_INDUSTRY_KEYWORDS = ["Software", "IT Services"]

# Blue-chip benchmark tickers per sector for market-level averages.
# These are fetched each scan to compute "true" sector averages,
# avoiding the bias of averaging only 52-week-low stocks.
SECTOR_BENCHMARK_TICKERS = {
    "Financial Services": ["JPM", "BAC", "GS", "MS", "BRK-B"],
    "Healthcare": ["JNJ", "UNH", "PFE", "MRK", "LLY"],
    "Technology": ["AAPL", "MSFT", "GOOGL", "NVDA", "CRM"],
    "Consumer Defensive": ["PG", "KO", "PEP", "WMT", "COST"],
    "Consumer Cyclical": ["AMZN", "TSLA", "HD", "NKE", "MCD"],
    "Industrials": ["CAT", "HON", "UPS", "GE", "RTX"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG"],
    "Communication Services": ["GOOGL", "META", "DIS", "T", "VZ"],
    "Basic Materials": ["LIN", "APD", "ECL", "NEM", "FCX"],
    "Utilities": ["NEE", "DUK", "SO", "AEP", "D"],
    "Real Estate": ["PLD", "AMT", "EQIX", "SPG", "O"],
}

# Damodaran blend
USE_DAMODARAN_BLEND = os.getenv("USE_DAMODARAN_BLEND", "true").lower() == "true"

# China ADR configurable penalty
CHINA_ADR_PENALTY = int(os.getenv("CHINA_ADR_PENALTY", "-20"))

# Notifications
NOTIFY_ENABLED = os.getenv("NOTIFY_ENABLED", "false").lower() == "true"
NOTIFY_TOP_N = int(os.getenv("NOTIFY_TOP_N", "10"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
NOTIFY_FROM = os.getenv("NOTIFY_FROM", "")
NOTIFY_TO = os.getenv("NOTIFY_TO", "")

# API settings key (set to protect /api/settings endpoint)
SETTINGS_API_KEY = os.getenv("SETTINGS_API_KEY", "")

# Scoring thresholds
OUTLIER_FPE_MAX = 100
OUTLIER_PB_MAX = 20
OUTLIER_EV_EBITDA_MAX = 50

# Scheduler
DAILY_REFRESH_HOUR = 16  # 4 PM ET
DAILY_REFRESH_MINUTE = 30

# Server
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
