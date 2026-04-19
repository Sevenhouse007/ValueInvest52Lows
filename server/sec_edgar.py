"""SEC EDGAR insider-transactions fallback.

Used when Yahoo's `insiderTransactions` module is empty (common for foreign
listings / ADRs). Fetches recent Form 4 filings, parses transaction codes
(P=purchase, S=sale) from the XML body, and returns aggregate buy/sell
counts and net share change for the lookback window.

Notes:
- SEC requires a User-Agent header that identifies the requester.
- SEC rate limit is ~10 req/s; we sleep 110ms between Form 4 fetches.
- The ticker→CIK map (~5MB) is cached in-process for 24h.
- Per-ticker insider results are cached in-process for the current day so
  the same scan or repeated lookups don't re-hit EDGAR.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# SEC requires a real contact email in the User-Agent.
USER_AGENT = "ValueInvest52Lows scanner.sasfaw.com sasfaw@gmail.com"
SEC_HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}

_TICKER_TO_CIK: dict[str, str] = {}
_TICKER_LOADED_AT: float = 0
_TICKER_TTL = 86400  # 24h

# Per-ticker insider cache: ticker -> (date_str, (buys, sells, net_shares) | None)
_INSIDER_CACHE: dict[str, tuple[str, Optional[tuple[int, int, int]]]] = {}

# Cap how many recent Form 4 filings we parse per ticker. Insider activity in
# the last 6 months almost always lives in the top ~30 most recent filings.
_MAX_FORM4_PER_TICKER = 30
_RATE_LIMIT_SLEEP = 0.11  # ~9 req/s, under SEC's 10 req/s cap


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _load_ticker_map() -> None:
    """Load (and cache for 24h) SEC's ticker→CIK mapping."""
    global _TICKER_TO_CIK, _TICKER_LOADED_AT
    if _TICKER_TO_CIK and (time.time() - _TICKER_LOADED_AT) < _TICKER_TTL:
        return
    try:
        r = httpx.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS,
            timeout=20.0,
        )
        r.raise_for_status()
        data = r.json()
        # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
        m = {
            entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
            for entry in data.values()
            if "ticker" in entry and "cik_str" in entry
        }
        _TICKER_TO_CIK = m
        _TICKER_LOADED_AT = time.time()
        logger.info(f"Loaded SEC ticker→CIK map: {len(m)} entries")
    except Exception as e:
        logger.warning(f"SEC ticker map load failed: {e}")


def get_cik(ticker: str) -> Optional[str]:
    """Return zero-padded 10-digit CIK for ticker, or None if not registered."""
    _load_ticker_map()
    return _TICKER_TO_CIK.get(ticker.upper())


def _parse_form4_xml(xml_bytes: bytes) -> tuple[int, int, int]:
    """Return (buys, sells, net_shares) parsed from a single Form 4 XML body."""
    buys = sells = 0
    net_shares = 0
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return 0, 0, 0

    # nonDerivativeTransaction = direct buy/sell of the underlying stock.
    # Derivatives (options, RSUs) are excluded as they're noisier signals.
    for txn in root.iter("nonDerivativeTransaction"):
        code_el = txn.find(".//transactionCode")
        shares_el = txn.find(".//transactionShares/value")
        if code_el is None or shares_el is None:
            continue
        code = (code_el.text or "").strip()
        try:
            shares = int(float(shares_el.text or 0))
        except (ValueError, TypeError):
            continue
        if code == "P":  # Open-market or private purchase
            buys += 1
            net_shares += shares
        elif code == "S":  # Open-market or private sale
            sells += 1
            net_shares -= shares
    return buys, sells, net_shares


def _find_form4_xml_name(items: list[dict]) -> Optional[str]:
    """Form 4 XML filenames are inconsistent. Try common patterns."""
    # Most recent filings: actual XML body is the .xml file (not the xsl* viewer).
    # Skip xslF345X*.xml (those are the rendering stylesheets).
    candidates = [
        it["name"] for it in items
        if it.get("name", "").endswith(".xml")
        and not it["name"].startswith("xsl")
    ]
    if not candidates:
        return None
    # Prefer the longest (most likely the actual Form 4 body, not a wrapper).
    return max(candidates, key=len)


def get_insider_activity(
    ticker: str,
    days: int = 180,
    max_filings: int = _MAX_FORM4_PER_TICKER,
) -> Optional[tuple[int, int, int]]:
    """Return (buy_count, sell_count, net_shares) from Form 4 filings in the
    last `days` days, or None if no data / fetch failed.
    """
    today = _today()
    cached = _INSIDER_CACHE.get(ticker.upper())
    if cached and cached[0] == today:
        return cached[1]

    cik = get_cik(ticker)
    if not cik:
        _INSIDER_CACHE[ticker.upper()] = (today, None)
        return None

    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    cik_int = int(cik)

    try:
        with httpx.Client(headers=SEC_HEADERS, timeout=15.0) as client:
            r = client.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
            r.raise_for_status()
            sub = r.json()
            recent = sub.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            accs = recent.get("accessionNumber", [])
            dates = recent.get("filingDate", [])

            buys = sells = 0
            net_shares = 0
            parsed = 0
            for form, acc, date in zip(forms, accs, dates):
                if parsed >= max_filings:
                    break
                if form not in ("4", "4/A"):
                    continue
                if date < cutoff:
                    continue

                acc_dashless = acc.replace("-", "")
                idx_url = (
                    f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
                    f"{acc_dashless}/index.json"
                )
                try:
                    fr = client.get(idx_url)
                    fr.raise_for_status()
                    idx = fr.json()
                    items = idx.get("directory", {}).get("item", [])
                    xml_name = _find_form4_xml_name(items)
                    if not xml_name:
                        time.sleep(_RATE_LIMIT_SLEEP)
                        continue

                    xml_url = (
                        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
                        f"{acc_dashless}/{xml_name}"
                    )
                    xr = client.get(xml_url)
                    xr.raise_for_status()
                    b, s, ns = _parse_form4_xml(xr.content)
                    buys += b
                    sells += s
                    net_shares += ns
                    parsed += 1
                    time.sleep(_RATE_LIMIT_SLEEP)
                except Exception as e:
                    logger.debug(f"SEC Form 4 parse failed for {ticker} {acc}: {e}")
                    time.sleep(_RATE_LIMIT_SLEEP)
                    continue

            result: Optional[tuple[int, int, int]]
            if buys + sells == 0:
                result = None
            else:
                result = (buys, sells, net_shares)
            _INSIDER_CACHE[ticker.upper()] = (today, result)
            if result:
                logger.info(
                    f"SEC EDGAR insider for {ticker}: "
                    f"{buys}B/{sells}S net={net_shares:+d} (parsed {parsed} Form 4s)"
                )
            return result

    except Exception as e:
        logger.warning(f"SEC EDGAR fetch failed for {ticker}: {e}")
        _INSIDER_CACHE[ticker.upper()] = (today, None)
        return None
