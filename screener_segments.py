"""Scrape product/business **segment revenue** for NSE stocks from screener.in,
for the drawer's revenue-breakup pie chart.

screener.in's ``/api/segments/{companyId}/profit-loss/1/`` endpoint is the only
clean, scriptable source that maps off the NSE symbol and returns per-segment
Sales. **The segment numbers are premium-gated**: without a screener.in premium
``sessionid`` cookie the value cells render "upgrade to premium" (we detect that
and degrade to segment *names* only, or nothing). Provide the cookie via the
``SCREENER_SESSION_COOKIE`` env var / GitHub secret to populate real numbers.

Segment coverage is inherently partial (~50-65% of a broad NSE list): under
Ind-AS 108 many companies report only one segment, which we surface as
"single reportable segment" rather than a one-slice pie.

Results cache in reports/segments_cache.json (keyed by screener symbol); segment
data only changes quarterly, so a stale entry is refetched at most every
STALE_DAYS. Safe no-op when the cookie is absent or requests/bs4 are missing.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
except Exception:  # pragma: no cover
    requests = None
try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None

BASE = "https://www.screener.in"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "reports" / "segments_cache.json"
STALE_DAYS = 25         # segment disclosures change only quarterly
MAX_WORKERS = 2         # be polite to screener.in
BASE_DELAY = 1.2        # seconds between requests per worker (jitter added)

# segment rows that are accounting adjustments, not real business lines
_DROP = ("unallocated", "less: inter", "intersegment", "elimination", "total", "others total")


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


def _cookie_header(raw: str) -> str:
    """Accept either a full cookie string ('sessionid=abc; csrftoken=...') or a
    bare sessionid value."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    return raw if "=" in raw else f"sessionid={raw}"


def _get(session, url, tries=3):
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
        return None  # 404 etc.: don't retry
    return None


def _company_id(session, screener_sym: str):
    """Map an NSE symbol to screener's numeric companyId via the public search
    API. Hyphenated symbols (BAJAJ-AUTO) must be queried space-separated."""
    query = screener_sym.replace("-", " ")
    raw = _get(session, f"{BASE}/api/company/search/?q={requests.utils.quote(query)}")
    if not raw:
        return None
    try:
        results = json.loads(raw)
    except Exception:
        return None
    if not results:
        return None
    # Prefer the result whose /company/<SLUG>/ slug equals our symbol.
    for r in results:
        slug = (r.get("url") or "").strip("/").split("/")
        if len(slug) >= 2 and slug[0] == "company" and slug[1].upper() == screener_sym:
            return r.get("id")
    return results[0].get("id")


def parse_segments(html: str) -> dict:
    """Parse a screener /api/segments/ HTML fragment into
    {"segments": [{name, value, pct}], "names": [...], "gated": bool, "period": str}.

    Reads the newest period column of the "Sales" segment-line. Handles both the
    premium flat-row layout (name + per-period value cells) and the gated
    nested-table layout (names only, values behind the paywall)."""
    empty = {"segments": [], "names": [], "gated": False, "period": ""}
    if not html or BeautifulSoup is None:
        return empty
    soup = BeautifulSoup(html, "html.parser")
    # The revenue segment line is "Sales" for most companies but "Revenue" for
    # banks/NBFCs; fall back to the first segment-line tbody either way.
    tbody = (
        soup.select_one('tbody[data-segment-line="Sales"]')
        or soup.select_one('tbody[data-segment-line="Revenue"]')
        or soup.select_one("tbody[data-segment-line]")
    )
    if tbody is None:
        return empty
    line_label = (tbody.get("data-segment-line") or "").strip().lower()
    gated = "upgrade to premium" in tbody.get_text(" ", strip=True).lower()
    thead = soup.select_one("table.data-table thead")
    periods = [th.get_text(strip=True) for th in thead.find_all("th")][1:] if thead else []
    period = periods[-1] if periods else ""

    # screener's standard data-table row: <td class="text">Label</td><td>v1</td>…
    # Read each segment row's own cells (recursive=False avoids the nested
    # name-table screener renders in the gated/paywalled layout).
    # Keep single-cell rows too: the gated layout nests segment NAMES in
    # value-less rows (premium fills the value cells). So we still surface names
    # for the gated fallback, and read values wherever they exist.
    rows = []  # (name, latest_value_or_None)
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if not cells:
            continue
        label_cell = cells[0]
        if "text" not in (label_cell.get("class") or []):
            continue  # wrapper/spacer rows (e.g. the gated colspan row)
        name = label_cell.get_text(strip=True).replace("\xa0", " ").rstrip("+").strip()
        if not name or name.lower() == line_label or "strong" in (tr.get("class") or []):
            continue  # the "Sales"/"Revenue" group-total header row
        vals = [_to_float(c.get_text(strip=True)) for c in cells[1:]]
        latest = next((v for v in reversed(vals) if v is not None), None)
        rows.append((name, latest))

    rows = [(n, v) for (n, v) in rows if not any(d in n.lower() for d in _DROP)]
    names = [n for (n, _) in rows]
    priced = [(n, v) for (n, v) in rows if v is not None and v > 0]
    if priced and not gated:
        total = sum(v for _, v in priced)
        segments = [
            {"name": n, "value": round(v, 2), "pct": round(v / total * 100, 1)}
            for (n, v) in priced
        ]
        return {"segments": segments, "names": names, "gated": False, "period": period}
    return {"segments": [], "names": names, "gated": gated, "period": period}


def _fetch_one(session, screener_sym: str) -> dict:
    cid = _company_id(session, screener_sym)
    if cid is None:
        return {"segments": [], "names": [], "gated": False, "period": "", "_id": None}
    html = _get(session, f"{BASE}/api/segments/{cid}/profit-loss/1/")
    data = parse_segments(html or "")
    data["_id"] = cid
    return data


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fresh(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    scraped = entry.get("_scraped")
    if not scraped:
        return False
    try:
        return (dt.date.today() - dt.date.fromisoformat(scraped)).days < STALE_DAYS
    except ValueError:
        return False


def fetch_segments_map(symbols: list[str], display_symbols: list[str] | None = None) -> dict:
    """Return {nse_symbol: {"segments":[...], "names":[...], "gated":bool,
    "period":str}} for the given symbols. No-op (returns {}) unless
    SCREENER_SESSION_COOKIE is set, so CI without the secret just skips it and
    the drawer shows a graceful fallback."""
    cookie = _cookie_header(os.getenv("SCREENER_SESSION_COOKIE", ""))
    if not cookie:
        print("screener_segments: SCREENER_SESSION_COOKIE not set — skipping segment scrape.")
        return {}
    if requests is None or BeautifulSoup is None:
        print("screener_segments: requests/bs4 unavailable — skipping.")
        return {}

    display_symbols = display_symbols or symbols
    # nse symbol (BAJAJ-AUTO) -> screener symbol (same, sans .NS)
    pairs = {}
    for sym, dsym in zip(symbols, display_symbols):
        ssym = _screener_symbol(sym, dsym)
        if ssym:
            pairs[ssym] = sym
    screener_syms = list(pairs)

    cache = _load_cache()
    todo = [s for s in screener_syms if not _fresh(cache.get(s))]

    def worker(ssym):
        session = requests.Session()
        session.headers.update({
            "User-Agent": UA,
            "Referer": BASE + "/",
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": cookie,
        })
        time.sleep(BASE_DELAY + random.uniform(0, 1.0))
        try:
            data = _fetch_one(session, ssym)
        except Exception as exc:
            print(f"screener_segments: {ssym} failed: {exc}")
            data = {"segments": [], "names": [], "gated": False, "period": "", "_id": None}
        data["_scraped"] = dt.date.today().isoformat()
        return ssym, data

    if todo:
        print(f"screener_segments: fetching {len(todo)}/{len(screener_syms)} symbols "
              f"({len(screener_syms) - len(todo)} fresh in cache)...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for future in as_completed([pool.submit(worker, s) for s in todo]):
                ssym, data = future.result()
                cache[ssym] = data
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")

    # Build the public map keyed by NSE symbol, stripping private (_id/_scraped) keys.
    n_priced = 0
    out = {}
    for ssym, nse_sym in pairs.items():
        entry = cache.get(ssym) or {}
        segments = entry.get("segments") or []
        if segments:
            n_priced += 1
        out[nse_sym] = {
            "segments": segments,
            "names": entry.get("names") or [],
            "gated": bool(entry.get("gated")),
            "period": entry.get("period") or "",
        }
    print(f"screener_segments: {n_priced}/{len(out)} stocks have priced segment breakups.")
    return out
