"""Fundamentals from screener.in's PUBLIC company page (no login required).

Yahoo (yfinance) returns sparse fundamentals for many NSE names, which left the
per-stock SWOT thin. screener.in's public company page exposes a far more
complete picture — Stock P/E, ROE, ROCE, dividend yield, book value, promoter
holding, quarterly sales/profit growth, margins, debt/equity and free cash flow
— all without a session cookie, so this is safe to run in CI.

`add_screener_fundamentals(stocks)` enriches the leading-stock DataFrame in the
same schema as `add_yahoo_fundamentals`, overriding numeric fundamental columns
where screener has a value and leaving Yahoo's values (and text fields like the
company description/website) untouched where it does not. Results are cached in
reports/screener_fundamentals_cache.json (committed by the workflow) so daily
runs only refetch stale/missing symbols and stay polite to screener.in.
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
except ImportError:  # pragma: no cover - requests ships transitively with yfinance
    requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

BASE = "https://www.screener.in"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "reports" / "screener_fundamentals_cache.json"
STALE_DAYS = 4          # refetch a cached symbol only if older than this
MAX_WORKERS = 3         # small pool — be polite to screener.in
BASE_DELAY = 1.5        # seconds between requests per worker (jitter added)

# screener top-ratios label -> our field
_TOP = {
    "Stock P/E": "pe_ratio",
    "ROE": "roe_pct",
    "ROCE": "roce",
    "Dividend Yield": "dividend_yield_pct",
    "Book Value": "book_value",
    "Current Price": "current_price",
    "Market Cap": "market_cap_cr",
}

# numeric fields screener is authoritative for (override Yahoo when present)
SCREENER_FIELDS = [
    "pe_ratio", "pb_ratio", "roe_pct", "roce", "dividend_yield_pct",
    "net_margin_pct", "operating_margin_pct", "eps", "debt_to_equity",
    "free_cash_flow_cr", "promoter_or_insider_holding_pct",
    "qtr_sales_var_pct", "qtr_profit_var_pct",
]


def _to_float(text):
    if text is None:
        return None
    s = str(text).replace(",", "").replace("%", "").replace("₹", "").strip()
    if s in ("", "-", "NaN", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _screener_symbol(symbol: str, display_symbol: str = "") -> str:
    sym = (display_symbol or symbol or "").strip().upper()
    if sym.endswith(".NS"):
        sym = sym[:-3]
    return sym


def _get(session, url, tries=3):
    """GET with backoff; retries transient throttling (429/5xx) so a CI burst
    against screener.in doesn't silently drop symbols."""
    for attempt in range(tries):
        try:
            resp = session.get(url, timeout=25)
        except Exception:
            time.sleep(2 * (attempt + 1) + random.uniform(0, 1))
            continue
        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (429, 500, 502, 503, 504):
            retry_after = resp.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else 3 * (attempt + 1)
            time.sleep(min(wait, 20) + random.uniform(0, 1))
            continue
        return None  # 404 and friends: no point retrying
    return None


def _has_top_data(html: str) -> bool:
    """True only if the top-ratios box has actual numbers (a company's
    consolidated page can exist but be empty — the data is on standalone)."""
    if not html or 'id="top-ratios"' not in html or BeautifulSoup is None:
        return False
    ul = BeautifulSoup(html, "html.parser").find("ul", id="top-ratios")
    if not ul:
        return False
    return any((num := li.find("span", class_="number")) and num.get_text(strip=True)
               for li in ul.find_all("li"))


def _fetch_html(session, symbol: str):
    """Prefer the consolidated page, but fall back to standalone when
    consolidated has no populated ratios (common for banks/NBFCs)."""
    fallback = None
    for suffix in ("/consolidated/", "/"):
        html = _get(session, f"{BASE}/company/{symbol}{suffix}")
        if not html:
            continue
        if _has_top_data(html):
            return html
        fallback = fallback or html
    return fallback


def _section_rows(soup, section_id):
    """{clean_label: [floats...]} for a screener statement section."""
    sec = soup.find("section", id=section_id)
    if not sec:
        return {}
    table = sec.find("table")
    if not table or not table.find("tbody"):
        return {}
    rows = {}
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True).replace("\xa0", " ").rstrip("+").strip()
        rows[label] = [_to_float(c.get_text()) for c in cells[1:]]
    return rows


def _last(values):
    for v in reversed(values or []):
        if v is not None:
            return v
    return None


def _yoy(values):
    """Latest vs the same period 4 columns back (year-on-year %), if possible."""
    vals = values or []
    if len(vals) < 5:
        return None
    latest, year_ago = vals[-1], vals[-5]
    if latest is None or year_ago is None or year_ago == 0:
        return None
    return round((latest - year_ago) / abs(year_ago) * 100, 1)


def _parse(html: str) -> dict:
    out = {}
    if not html or BeautifulSoup is None:
        return out
    soup = BeautifulSoup(html, "html.parser")

    # --- top ratios box ---
    top = {}
    ul = soup.find("ul", id="top-ratios")
    if ul:
        for li in ul.find_all("li"):
            name_el = li.find("span", class_="name")
            num_el = li.find("span", class_="number")
            if not name_el:
                continue
            field = _TOP.get(name_el.get_text(strip=True))
            if field:
                top[field] = _to_float(num_el.get_text() if num_el else None)
    for f in ("pe_ratio", "roe_pct", "roce", "dividend_yield_pct"):
        if top.get(f) is not None:
            out[f] = top[f]
    if top.get("current_price") and top.get("book_value"):
        out["pb_ratio"] = round(top["current_price"] / top["book_value"], 2)

    # --- annual P&L: margins + EPS ---
    # banks/NBFCs label the top line "Revenue" (not "Sales") and show
    # "Financing Margin %" (not "OPM %").
    pnl = _section_rows(soup, "profit-loss")
    is_financial = "Sales" not in pnl and ("Revenue" in pnl or "Financing Profit" in pnl)
    topline = _last(pnl.get("Sales")) if "Sales" in pnl else _last(pnl.get("Revenue"))
    net_profit = _last(pnl.get("Net Profit"))
    if topline and net_profit is not None:
        out["net_margin_pct"] = round(net_profit / topline * 100, 1)
    opm = _last(pnl.get("OPM %")) if "OPM %" in pnl else _last(pnl.get("Financing Margin %"))
    if opm is not None:
        out["operating_margin_pct"] = opm
    eps = _last(pnl.get("EPS in Rs"))
    if eps is not None:
        out["eps"] = eps

    # --- balance sheet: debt/equity (x100 to match Yahoo's convention) ---
    # skip for banks/NBFCs — borrowings are their raw material, so the ratio is
    # structurally huge and would wrongly read as a red flag in the SWOT.
    if not is_financial:
        bs = _section_rows(soup, "balance-sheet")
        borrow = _last(bs.get("Borrowings"))
        equity = _last(bs.get("Equity Capital"))
        reserves = _last(bs.get("Reserves"))
        if borrow is not None and equity is not None and reserves is not None:
            networth = equity + reserves
            if networth > 0:
                out["debt_to_equity"] = round(borrow / networth * 100, 1)

    # --- cash flow: screener gives Free Cash Flow directly ---
    # skip for banks/NBFCs — lending is their investing outflow, so FCF is
    # structurally negative and would wrongly read as a weakness in the SWOT.
    if not is_financial:
        cf = _section_rows(soup, "cash-flow")
        fcf = _last(cf.get("Free Cash Flow"))
        if fcf is None:  # fallback proxy: CFO + CFI
            cfo, cfi = _last(cf.get("Cash from Operating Activity")), _last(cf.get("Cash from Investing Activity"))
            if cfo is not None and cfi is not None:
                fcf = cfo + cfi
        if fcf is not None:
            out["free_cash_flow_cr"] = round(fcf, 1)

    # --- shareholding: promoter holding ---
    shp = _section_rows(soup, "shareholding")
    promoters = _last(shp.get("Promoters"))
    if promoters is not None:
        out["promoter_or_insider_holding_pct"] = promoters

    # --- quarters: YoY sales/profit growth (banks label it "Revenue") ---
    q = _section_rows(soup, "quarters")
    qsg = _yoy(q.get("Sales") if "Sales" in q else q.get("Revenue"))
    if qsg is not None:
        out["qtr_sales_var_pct"] = qsg
    qpg = _yoy(q.get("Net Profit"))
    if qpg is not None:
        out["qtr_profit_var_pct"] = qpg

    return out


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fresh(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    # an empty result (fetch failed / page had no ratios) should retry next run,
    # not sit stale for STALE_DAYS
    if not any(not k.startswith("_") for k in entry):
        return False
    scraped = entry.get("_scraped")
    if not scraped:
        return False
    try:
        age = (dt.date.today() - dt.date.fromisoformat(scraped)).days
    except ValueError:
        return False
    return age < STALE_DAYS


def add_screener_fundamentals(stocks: pd.DataFrame) -> pd.DataFrame:
    """Override numeric fundamental columns with screener.in values where present."""
    stocks = stocks.copy()
    if stocks.empty or os.getenv("SKIP_SCREENER_FUNDAMENTALS") == "1":
        return stocks
    if requests is None or BeautifulSoup is None:
        print("screener_fundamentals: requests/bs4 unavailable — skipping.")
        return stocks

    # map DataFrame symbol -> screener symbol
    disp = stocks["display_symbol"] if "display_symbol" in stocks.columns else stocks["symbol"]
    pairs = {}
    for sym, dsym in zip(stocks["symbol"].fillna(""), disp.fillna("")):
        s = _screener_symbol(sym, dsym)
        if s:
            pairs.setdefault(s, None)
    screener_syms = list(pairs)

    cache = _load_cache()
    todo = [s for s in screener_syms if not _fresh(cache.get(s))]

    def worker(sym):
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Referer": BASE + "/"})
        time.sleep(BASE_DELAY + random.uniform(0, 1.0))
        try:
            data = _parse(_fetch_html(session, sym))
        except Exception as exc:  # never let one symbol break the build
            print(f"screener_fundamentals: {sym} failed: {exc}")
            data = {}
        data["_scraped"] = dt.date.today().isoformat()
        return sym, data

    if todo:
        print(f"screener_fundamentals: fetching {len(todo)}/{len(screener_syms)} symbols "
              f"({len(screener_syms) - len(todo)} fresh in cache)...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for future in as_completed([pool.submit(worker, s) for s in todo]):
                sym, data = future.result()
                cache[sym] = data
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")

    # screener symbol per row (once), then override each field from the cache
    has_disp = "display_symbol" in stocks.columns
    row_syms = [
        _screener_symbol(sym, dsym if has_disp else "")
        for sym, dsym in zip(stocks["symbol"].fillna(""),
                             (stocks["display_symbol"] if has_disp else stocks["symbol"]).fillna(""))
    ]
    sym_series = pd.Series(row_syms, index=stocks.index)

    filled = 0
    for field in SCREENER_FIELDS:
        new = pd.to_numeric(
            sym_series.map(lambda k: (cache.get(k) or {}).get(field)),
            errors="coerce",
        )
        if field not in stocks.columns:
            stocks[field] = np.nan
        existing = pd.to_numeric(stocks[field], errors="coerce")
        stocks[field] = new.combine_first(existing)  # screener wins where present
        filled += int(new.notna().sum())
    print(f"screener_fundamentals: applied {filled} field values across {len(stocks)} stocks.")
    return stocks


if __name__ == "__main__":
    import sys
    syms = sys.argv[1:] or ["RELIANCE", "BAJAJ-AUTO"]
    df = pd.DataFrame({"symbol": [s + ".NS" for s in syms], "display_symbol": syms})
    out = add_screener_fundamentals(df)
    cols = ["display_symbol"] + [c for c in SCREENER_FIELDS if c in out.columns]
    print(out[cols].to_string(index=False))
