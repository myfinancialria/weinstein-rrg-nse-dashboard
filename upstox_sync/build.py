"""Build dashboard/upstox_data.json — fundamentals + options + FII/DII.

Split cadence: options and FII/DII refresh every run; fundamentals (quarterly data)
refresh only when older than ``fund_max_age_days`` or when ``--fundamentals`` forces it,
reusing the previously-committed block otherwise. Every network call is defensive —
a failure on one stock/section leaves it absent with a note, never sinks the run.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

from .client import UpstoxClient
from .resolver import Instrument, Resolver

IST = timezone(timedelta(hours=5, minutes=30))

# FII is reported for F&O + cash segments; DII meaningfully only for cash.
FII_SEGMENTS = ["NSE_FO|INDEX_FUTURES", "NSE_FO|STOCK_FUTURES",
                "NSE_FO|INDEX_OPTIONS", "NSE_FO|STOCK_OPTIONS", "NSE_EQ|CASH"]
DII_SEGMENTS = ["NSE_EQ|CASH"]

INDEX_KEYS = {"NIFTY 50": "NSE_INDEX|Nifty 50", "BANK NIFTY": "NSE_INDEX|Nifty Bank"}


def now_ist_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def today_ist() -> date:
    return datetime.now(IST).date()


def last_trading_date(d: date | None = None) -> str:
    d = d or today_ist()
    while d.weekday() >= 5:  # Sat/Sun -> back to Friday
        d -= timedelta(days=1)
    return d.isoformat()


def _num(v):
    try:
        return float(str(v).replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _ratio(ratios: list, name: str):
    for r in ratios or []:
        if r.get("name") == name:
            return r.get("company_value")
    return None


def _latest(hist: list):
    return hist[0] if hist else None


def derive_ratios(ratios: list, income: dict, holdings: list) -> dict:
    """Compact headline ratios that override the dashboard's stock fields."""
    out: dict = {}
    m = {"pe_ratio": "P/E", "pb_ratio": "P/B", "roe_pct": "ROE",
         "roce": "ROCE", "roa_pct": "ROA", "ev_ebitda": "EV/EBITDA"}
    for k, nm in m.items():
        v = _num(_ratio(ratios, nm))
        if v is not None:
            out[k] = v
    # margins from the latest annual income statement
    rows = {r.get("category"): r for r in income.get("income_statement", [])}
    rev = _latest((rows.get("revenue") or {}).get("history", []))
    npr = _latest((rows.get("net_profit") or {}).get("history", []))
    opr = _latest((rows.get("operating_profit") or {}).get("history", []))
    if rev and rev.get("value"):
        if npr and npr.get("value") is not None:
            out["net_margin_pct"] = round(npr["value"] / rev["value"] * 100, 2)
        if opr and opr.get("value") is not None:
            out["operating_margin_pct"] = round(opr["value"] / rev["value"] * 100, 2)
    # promoter holding from the latest shareholding snapshot
    for c in holdings or []:
        if str(c.get("category", "")).lower().startswith("promoter"):
            lh = _latest(c.get("history", []))
            if lh and lh.get("value") is not None:
                out["promoter_or_insider_holding_pct"] = lh["value"]
            break
    return out


def _try(section, fn, notes, default):
    try:
        return fn()
    except Exception as e:
        notes.append(f"{section}:{type(e).__name__}")
        return default


def build_fundamentals(client: UpstoxClient, inst: Instrument) -> dict:
    notes: list[str] = []
    isin, ikey = inst.isin, inst.instrument_key
    profile = _try("profile", lambda: client.profile(isin), notes, {})
    ratios = _try("key_ratios", lambda: client.key_ratios(isin), notes, [])
    income = _try("income", lambda: client.income_statement(isin), notes, {})
    balance = _try("balance", lambda: client.balance_sheet(isin), notes, {})
    cashflow = _try("cashflow", lambda: client.cash_flow(isin), notes, {})
    holdings = _try("holdings", lambda: client.share_holdings(isin), notes, [])
    actions = _try("actions", lambda: client.corporate_actions(isin), notes, [])
    peers_raw = _try("competitors", lambda: client.competitors(ikey), notes, [])
    peers = [{"name": c.get("name") or ((c.get("company_profile") or "").split(" is ")[0][:60]),
              "instrument_key": c.get("instrument_key") or ""} for c in (peers_raw or [])]
    return {
        "isin": isin, "instrument_key": ikey, "has_options": inst.has_options,
        "profile": profile, "ratios_full": ratios,
        "income_statement": income, "balance_sheet": balance, "cash_flow": cashflow,
        "share_holdings": holdings, "corporate_actions": actions, "competitors": peers,
        "ratios": derive_ratios(ratios, income, holdings),
        "notes": notes,
    }


def build_options(client: UpstoxClient, ikey: str, trade_date: str,
                  expiry: str = "current_month") -> dict | None:
    notes: list[str] = []
    pcr = _try("pcr", lambda: client.pcr(ikey, trade_date, expiry), notes, None)
    mp = _try("max_pain", lambda: client.max_pain(ikey, trade_date, expiry), notes, None)
    oi = _try("oi", lambda: client.oi(ikey, trade_date, expiry), notes, None)
    if not (pcr or mp or oi):
        return None
    strikes = []
    if oi and oi.get("call_put_oi_data_list"):
        for s in oi["call_put_oi_data_list"]:
            strikes.append({"strike": s.get("strike_price"),
                            "call_oi": s.get("call_oi"), "put_oi": s.get("put_oi")})
    return {
        "expiry": (pcr or mp or oi or {}).get("expiry_date") or (oi or {}).get("expiry"),
        "spot": (pcr or mp or oi or {}).get("spot_closing_price"),
        "pcr": (pcr or {}).get("pcr"),
        "max_pain": (mp or {}).get("max_pain"),
        "total_calls": (oi or {}).get("total_calls"),
        "total_puts": (oi or {}).get("total_puts"),
        "oi_by_strike": strikes,
        "date": trade_date,
    }


def _flow_summary(seg_series: list) -> dict | None:
    """Latest day's buy/sell/net (₹ crore) for one segment."""
    if not seg_series:
        return None
    row = seg_series[0] if seg_series[0].get("time_stamp") else seg_series[-1]
    # series may be oldest- or newest-first; pick the max timestamp
    row = max(seg_series, key=lambda r: r.get("time_stamp") or 0)
    buy, sell = row.get("buy_amount"), row.get("sell_amount")
    net = (buy - sell) if (buy is not None and sell is not None) else None
    return {"time_stamp": row.get("time_stamp"), "buy": buy, "sell": sell, "net": net,
            "oi_amount": row.get("oi_amount")}


def build_flows(client: UpstoxClient) -> dict:
    fii = _try("fii", lambda: client.fii(FII_SEGMENTS), [], {})
    dii = _try("dii", lambda: client.dii(DII_SEGMENTS), [], {})
    segments = {}
    for seg in FII_SEGMENTS:
        segments[seg] = {"fii": _flow_summary(fii.get(seg, []))}
    for seg in DII_SEGMENTS:
        segments.setdefault(seg, {})["dii"] = _flow_summary(dii.get(seg, []))
    return {"as_of": now_ist_iso(), "segments": segments}


def build_index_options(client: UpstoxClient, trade_date: str) -> dict:
    out = {}
    for name, key in INDEX_KEYS.items():
        o = _try(name, lambda k=key: build_options(client, k, trade_date), [], None)
        if o:
            out[name] = o
    return out


def _read_existing(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _fund_is_fresh(existing: dict, max_age_days: int) -> bool:
    ts = existing.get("fundamentals_generated_at")
    if not ts:
        return False
    try:
        gen = datetime.fromisoformat(ts).date()
    except ValueError:
        return False
    return (today_ist() - gen).days < max_age_days


def sync(client: UpstoxClient, symbols: list[tuple[str, str]], out_path: str | Path,
         data_dir: str | Path, *, force_fundamentals: bool = False,
         fund_max_age_days: int = 7, verbose: bool = True) -> dict:
    """symbols: list of (display_symbol, dashboard_symbol_with_NS)."""
    out_path = Path(out_path)
    trade_date = last_trading_date()
    resolver = Resolver(data_dir, trade_date)  # one master download per trading day
    existing = _read_existing(out_path)

    do_fund = force_fundamentals or not _fund_is_fresh(existing, fund_max_age_days)
    fundamentals = {} if do_fund else dict(existing.get("fundamentals", {}))
    options: dict = {}
    unresolved: list[str] = []

    if verbose:
        print(f"date={trade_date}  fundamentals={'REFRESH' if do_fund else 'reuse'}  "
              f"({len(symbols)} symbols)")

    for i, (disp, ns_sym) in enumerate(symbols, 1):
        inst = resolver.resolve(ns_sym)
        if not inst:
            unresolved.append(disp)
            continue
        try:
            if do_fund:
                fundamentals[disp] = build_fundamentals(client, inst)
            if inst.has_options:
                opt = build_options(client, inst.instrument_key, trade_date)
                if opt:
                    options[disp] = opt
        except Exception as e:  # one bad stock never sinks the run
            print(f"  ! {disp}: {type(e).__name__}: {e}")
        if verbose and i % 25 == 0:
            print(f"  {i}/{len(symbols)}…")

    payload = {
        "generated_at": now_ist_iso(),
        "options_date": trade_date,
        "fundamentals_generated_at": (now_ist_iso() if do_fund
                                      else existing.get("fundamentals_generated_at", now_ist_iso())),
        "fii_dii": build_flows(client),
        "index_options": build_index_options(client, trade_date),
        "fundamentals": fundamentals,
        "options": options,
        "unresolved": unresolved,
        "counts": {"fundamentals": len(fundamentals), "options": len(options),
                   "unresolved": len(unresolved)},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    if verbose:
        print(f"wrote {out_path}  fundamentals={len(fundamentals)} options={len(options)} "
              f"unresolved={len(unresolved)}")
    return payload
