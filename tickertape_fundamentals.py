"""Enrich stock fundamentals from Tickertape's public API (tickertape.in).

All endpoints used here are public — verified live with no cookie/auth:
  search   GET  /search?text=<SYM>&types=stock              -> sid (e.g. TIMK)
  ratios   POST /screener/query {project,sids}               -> pe/pb/roe/roce/…
  income   GET  /stocks/financials/income/<sid>/annual/normal?count=1
  income   GET  /stocks/financials/income/<sid>/interim/normal?count=5  (qtr YoY)
  cashflow GET  /stocks/financials/cashflow/<sid>/annual/normal?count=1  -> FCF
  holdings GET  /stocks/holdings/<sid>                       -> promoter %

Values override Yahoo/screener where present (Tickertape is the requested
fundamentals source). Cached in reports/tickertape_fundamentals_cache.json.
Safe no-op if requests is unavailable. Set SKIP_TICKERTAPE=1 to disable.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
STALE_DAYS = 4
MAX_WORKERS = 4
BASE_DELAY = 0.4

# dashboard field -> Tickertape advancedRatios key (POST /screener/query)
_RATIO_MAP = {
    "pe_ratio": "pe",
    "pb_ratio": "pb",
    "roe_pct": "roe",
    "roce": "roce",
    "dividend_yield_pct": "divYield",
    "net_margin_pct": "pftMrg",
    "market_cap_cr": "mrktCapf",
    # debt_to_equity handled separately (Tickertape gives a raw ratio; the
    # dashboard/Yahoo convention is x100).
}
_RATIO_PROJECT = list(dict.fromkeys(list(_RATIO_MAP.values()) + ["dbtEqt"]))

# numeric fields Tickertape is authoritative for (override where present)
TICKERTAPE_FIELDS = [
    "pe_ratio", "pb_ratio", "roe_pct", "roce", "dividend_yield_pct",
    "net_margin_pct", "operating_margin_pct", "eps", "debt_to_equity",
    "free_cash_flow_cr", "promoter_or_insider_holding_pct", "market_cap_cr",
    "qtr_sales_var_pct", "qtr_profit_var_pct",
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


def _get(session, url, tries=3):
    for attempt in range(tries):
        try:
            resp = session.get(url, timeout=20)
        except Exception:
            time.sleep(1.5 * (attempt + 1) + random.uniform(0, 1))
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return None
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 * (attempt + 1) + random.uniform(0, 1))
            continue
        return None
    return None


def _post(session, url, body, tries=3):
    for attempt in range(tries):
        try:
            resp = session.post(url, json=body, timeout=20)
        except Exception:
            time.sleep(1.5 * (attempt + 1) + random.uniform(0, 1))
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return None
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 * (attempt + 1) + random.uniform(0, 1))
            continue
        return None
    return None


def _sid(session, tt_sym: str):
    data = _get(session, f"{BASE}/search?text={requests.utils.quote(tt_sym)}&types=stock")
    stocks = (((data or {}).get("data")) or {}).get("stocks") or []
    if not stocks:
        return None
    # Prefer the exact ticker match, else the top result.
    for s in stocks:
        if str(s.get("ticker", "")).upper() == tt_sym or s.get("match") == "EXACT":
            return s.get("sid")
    return stocks[0].get("sid")


def _income_row(session, sid, period):
    data = _get(session, f"{BASE}/stocks/financials/income/{sid}/{period}/normal?count=5")
    rows = (data or {}).get("data") or []
    return rows


def _fetch_one(session, tt_sym: str) -> dict:
    out = {}
    sid = _sid(session, tt_sym)
    if not sid:
        return out
    out["_sid"] = sid

    # --- ratios (pe/pb/roe/roce/divYield/net-margin/mcap/debt-equity) ---
    rq = _post(session, f"{BASE}/screener/query",
               {"match": {}, "project": _RATIO_PROJECT, "count": 1, "sids": [sid]})
    try:
        ratios = rq["data"]["results"][0]["stock"]["advancedRatios"]
    except Exception:
        ratios = {}
    for field, key in _RATIO_MAP.items():
        v = _num(ratios.get(key))
        if v is not None:
            out[field] = round(v, 2)
    de = _num(ratios.get("dbtEqt"))
    if de is not None:
        out["debt_to_equity"] = round(de * 100, 2)  # match Yahoo/screener x100 convention

    # --- annual income: EPS + operating (EBITDA) margin proxy ---
    annual = _income_row(session, sid, "annual")
    # newest annual row that isn't the appended TTM aggregate, else the TTM row
    latest = next((r for r in reversed(annual) if r.get("displayPeriod") != "TTM"), annual[-1] if annual else None)
    if latest:
        eps = _num(latest.get("incEps"))
        if eps is not None:
            out["eps"] = round(eps, 2)
        trev, ebi = _num(latest.get("incTrev")), _num(latest.get("incEbi"))
        if trev and ebi is not None and trev > 0:
            out["operating_margin_pct"] = round(ebi / trev * 100, 1)

    # --- quarterly income: YoY sales/profit growth (latest vs 4 quarters ago) ---
    interim = _income_row(session, sid, "interim")
    if len(interim) >= 5:
        cur, yago = interim[-1], interim[-5]
        cs, ys = _num(cur.get("incTrev")), _num(yago.get("incTrev"))
        if cs is not None and ys not in (None, 0):
            out["qtr_sales_var_pct"] = round((cs / ys - 1) * 100, 1)
        cp, yp = _num(cur.get("incNinc")), _num(yago.get("incNinc"))
        if cp is not None and yp not in (None, 0) and yp > 0:
            out["qtr_profit_var_pct"] = round((cp / yp - 1) * 100, 1)

    # --- cash flow: free cash flow ---
    cf = _get(session, f"{BASE}/stocks/financials/cashflow/{sid}/annual/normal?count=1")
    cfrows = (cf or {}).get("data") or []
    if cfrows:
        fcf = _num(cfrows[-1].get("cafFcf"))
        if fcf is not None:
            out["free_cash_flow_cr"] = round(fcf, 1)

    # --- shareholding: promoter holding (latest quarter) ---
    hold = _get(session, f"{BASE}/stocks/holdings/{sid}")
    hrows = (hold or {}).get("data") or []
    if hrows:
        pm = _num((hrows[-1].get("data") or {}).get("pmPctT"))
        if pm is not None:
            out["promoter_or_insider_holding_pct"] = round(pm, 2)

    return out


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fresh(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    if not any(not k.startswith("_") for k in entry):
        return False  # empty result -> retry next run
    scraped = entry.get("_scraped")
    if not scraped:
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

    disp = stocks["display_symbol"] if "display_symbol" in stocks.columns else stocks["symbol"]
    pairs = {}
    for sym, dsym in zip(stocks["symbol"].fillna(""), disp.fillna("")):
        s = _tt_symbol(sym, dsym)
        if s:
            pairs.setdefault(s, None)
    tt_syms = list(pairs)

    cache = _load_cache()
    todo = [s for s in tt_syms if not _fresh(cache.get(s))]

    def worker(sym):
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Referer": "https://www.tickertape.in/"})
        time.sleep(BASE_DELAY + random.uniform(0, 0.5))
        try:
            data = _fetch_one(session, sym)
        except Exception as exc:
            print(f"tickertape_fundamentals: {sym} failed: {exc}")
            data = {}
        data["_scraped"] = dt.date.today().isoformat()
        return sym, data

    if todo:
        print(f"tickertape_fundamentals: fetching {len(todo)}/{len(tt_syms)} symbols "
              f"({len(tt_syms) - len(todo)} fresh in cache)...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for future in as_completed([pool.submit(worker, s) for s in todo]):
                sym, data = future.result()
                cache[sym] = data
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")

    has_disp = "display_symbol" in stocks.columns
    row_syms = [
        _tt_symbol(sym, dsym if has_disp else "")
        for sym, dsym in zip(stocks["symbol"].fillna(""),
                             (stocks["display_symbol"] if has_disp else stocks["symbol"]).fillna(""))
    ]
    sym_series = pd.Series(row_syms, index=stocks.index)

    filled = 0
    for field in TICKERTAPE_FIELDS:
        new = pd.to_numeric(sym_series.map(lambda k: (cache.get(k) or {}).get(field)), errors="coerce")
        if field not in stocks.columns:
            stocks[field] = np.nan
        existing = pd.to_numeric(stocks[field], errors="coerce")
        stocks[field] = new.combine_first(existing)  # Tickertape wins where present
        filled += int(new.notna().sum())
    print(f"tickertape_fundamentals: applied {filled} field values across {len(stocks)} stocks.")
    return stocks
