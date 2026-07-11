"""Enrich stock fundamentals from Tickertape's public screener API.

Uses ONE bulk endpoint — `POST /screener/query` — paginated over the whole NSE
universe (~5,800 stocks, 500/page → ~12 requests), keyed by the NSE ticker in
`stock.info.ticker`. This avoids per-stock lookups entirely, so it scales to all
344 stocks without tripping Tickertape's request-rate limit (per-stock search
gets IP-limited fast and, worse, returns EMPTY under the limit — indistinguishable
from "not found").

All public — verified live, no cookie/auth. Fields pulled per stock:
  pe, pbr(P/B), roe, roce, pftMrg(net margin), opmg(operating margin),
  dbtEqt(debt/equity), divYield, mrktCapf(market cap ₹cr), incEps(EPS),
  cafFcf(free cash flow ₹cr), strown(promoter/strategic holding %).

Values override Yahoo/screener where present. The whole universe map is cached
in reports/tickertape_fundamentals_cache.json (refreshed every STALE_DAYS).
Safe no-op if requests is unavailable or SKIP_TICKERTAPE=1.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

BASE = "https://api.tickertape.in"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "reports" / "tickertape_fundamentals_cache.json"
STALE_DAYS = 2
PAGE = 500
MAX_PAGES = 30  # safety cap (~15k stocks)
DELAY = 0.5     # between pages

# dashboard field -> Tickertape advancedRatios key
_FIELD_MAP = {
    "pe_ratio": "pe",
    "pb_ratio": "pbr",
    "roe_pct": "roe",
    "roce": "roce",
    "net_margin_pct": "pftMrg",
    "operating_margin_pct": "opmg",
    "dividend_yield_pct": "divYield",
    "market_cap_cr": "mrktCapf",
    "eps": "incEps",
    "free_cash_flow_cr": "cafFcf",
    "promoter_or_insider_holding_pct": "strown",
    # debt_to_equity handled separately (Tickertape gives a raw ratio; the
    # dashboard/Yahoo convention is x100).
}
_PROJECT = list(dict.fromkeys(list(_FIELD_MAP.values()) + ["dbtEqt"]))

# numeric fields Tickertape is authoritative for (override where present)
TICKERTAPE_FIELDS = [
    "pe_ratio", "pb_ratio", "roe_pct", "roce", "dividend_yield_pct",
    "net_margin_pct", "operating_margin_pct", "eps", "debt_to_equity",
    "free_cash_flow_cr", "promoter_or_insider_holding_pct", "market_cap_cr",
]


def _num(x):
    try:
        if x is None:
            return None
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _tt_symbol(symbol: str, display_symbol: str = "") -> str:
    sym = (display_symbol or symbol or "").strip().upper()
    return sym[:-3] if sym.endswith(".NS") else sym


def _post(session, url, body, tries=4):
    for attempt in range(tries):
        try:
            resp = session.post(url, json=body, timeout=30)
        except Exception:
            time.sleep(2 * (attempt + 1) + random.uniform(0, 1))
            continue
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                return None
            # Under rate-limit the API returns {"message":"REQUEST_LIMIT_EXCEEDED"}
            # with a 200 — treat as transient and back off.
            if isinstance(data, dict) and data.get("message") == "REQUEST_LIMIT_EXCEEDED":
                time.sleep(5 * (attempt + 1) + random.uniform(0, 2))
                continue
            return data
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(3 * (attempt + 1) + random.uniform(0, 1))
            continue
        return None
    return None


def _fetch_universe(session) -> dict:
    """{NSE_TICKER: {advancedRatios}} for the whole screenable NSE universe."""
    out = {}
    offset = 0
    for _ in range(MAX_PAGES):
        data = _post(session, f"{BASE}/screener/query",
                     {"match": {}, "project": _PROJECT, "count": PAGE, "offset": offset})
        block = (data or {}).get("data") or {}
        results = block.get("results") or []
        if not results:
            break
        for r in results:
            st = r.get("stock") or {}
            ticker = ((st.get("info") or {}).get("ticker") or "").upper()
            if ticker:
                out[ticker] = st.get("advancedRatios") or {}
        total = (block.get("stats") or {}).get("count")
        offset += PAGE
        if total and offset >= total:
            break
        time.sleep(DELAY + random.uniform(0, 0.4))
    return out


def _fields_from_ratios(ar: dict) -> dict:
    out = {}
    for field, key in _FIELD_MAP.items():
        v = _num(ar.get(key))
        if v is not None:
            out[field] = round(v, 2)
    de = _num(ar.get("dbtEqt"))
    if de is not None:
        out["debt_to_equity"] = round(de * 100, 2)  # x100 to match Yahoo/screener convention
    return out


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cache_fresh(cache: dict) -> bool:
    scraped = cache.get("_scraped")
    if not scraped or not cache.get("universe"):
        return False
    try:
        return (dt.date.today() - dt.date.fromisoformat(scraped)).days < STALE_DAYS
    except ValueError:
        return False


def add_tickertape_fundamentals(stocks: pd.DataFrame) -> pd.DataFrame:
    """Override numeric fundamental columns with Tickertape values where present."""
    stocks = stocks.copy()
    if stocks.empty or os.getenv("SKIP_TICKERTAPE") == "1":
        return stocks
    if requests is None:
        print("tickertape_fundamentals: requests unavailable — skipping.")
        return stocks

    cache = _load_cache()
    if _cache_fresh(cache):
        universe = cache["universe"]
    else:
        session = requests.Session()
        session.headers.update({
            "User-Agent": UA, "Referer": "https://www.tickertape.in/",
            "Content-Type": "application/json", "Accept": "application/json",
        })
        universe = _fetch_universe(session)
        if universe:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(
                json.dumps({"_scraped": dt.date.today().isoformat(), "universe": universe}),
                encoding="utf-8",
            )
            print(f"tickertape_fundamentals: fetched {len(universe)} NSE tickers from screener/query.")
        else:
            universe = cache.get("universe") or {}  # fall back to stale on failure
            print("tickertape_fundamentals: bulk fetch failed — using cached universe.")

    has_disp = "display_symbol" in stocks.columns
    row_tickers = [
        _tt_symbol(sym, dsym if has_disp else "")
        for sym, dsym in zip(stocks["symbol"].fillna(""),
                             (stocks["display_symbol"] if has_disp else stocks["symbol"]).fillna(""))
    ]
    per_field = {f: [] for f in TICKERTAPE_FIELDS}
    for tk in row_tickers:
        fields = _fields_from_ratios(universe.get(tk) or {})
        for f in TICKERTAPE_FIELDS:
            per_field[f].append(fields.get(f))

    matched = sum(1 for tk in row_tickers if tk in universe)
    filled = 0
    for field in TICKERTAPE_FIELDS:
        new = pd.to_numeric(pd.Series(per_field[field], index=stocks.index), errors="coerce")
        if field not in stocks.columns:
            stocks[field] = np.nan
        existing = pd.to_numeric(stocks[field], errors="coerce")
        stocks[field] = new.combine_first(existing)  # Tickertape wins where present
        filled += int(new.notna().sum())
    print(f"tickertape_fundamentals: matched {matched}/{len(stocks)} stocks, applied {filled} field values.")
    return stocks
