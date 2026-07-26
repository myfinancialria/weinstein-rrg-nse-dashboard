"""CLI:

    python -m upstox_sync login [url|<code-or-redirect-url>]
    python -m upstox_sync probe                    # confirm API-family entitlement
    python -m upstox_sync one RELIANCE             # build one symbol (fundamentals+options), print
    python -m upstox_sync sync [--fundamentals] [--limit N] [--max-age-days 7]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import login as login_mod
from .build import (build_fundamentals, build_options, last_trading_date, sync)
from .client import NotEntitledError, TokenExpiredError, UpstoxClient, UpstoxError
from .resolver import Resolver

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
DATA_DIR = ROOT / "reports" / "upstox_cache"
OUT = DASH / "upstox_data.json"


def _token() -> str:
    token = login_mod.read_env().get("UPSTOX_TOKEN", "")
    if not token:
        raise SystemExit("No UPSTOX_TOKEN. Run `python -m upstox_sync login url` then `login <redirect-url>`.")
    return token


def _dashboard_symbols() -> list[tuple[str, str]]:
    """Return (display_symbol, symbol_with_NS) for every stock-level row."""
    dd = json.loads((DASH / "dashboard_data.json").read_text())
    out = []
    for s in dd.get("stocks", []):
        if s.get("level") != "stock":
            continue
        ns = s.get("symbol", "")
        disp = s.get("display_symbol") or ns.replace(".NS", "")
        if ns:
            out.append((disp, ns))
    return out


def cmd_login(args) -> int:
    login_mod.cli([args.arg or "url"])
    return 0


def cmd_probe(_args) -> int:
    date = last_trading_date()
    checks = [
        ("fundamentals/key-ratios", lambda c: c.key_ratios("INE002A01018")),
        ("fundamentals/income", lambda c: c.income_statement("INE002A01018")),
        ("options/pcr", lambda c: c.pcr("NSE_EQ|INE002A01018", date)),
        ("options/max-pain", lambda c: c.max_pain("NSE_EQ|INE002A01018", date)),
        ("options/oi", lambda c: c.oi("NSE_EQ|INE002A01018", date)),
        ("market/fii", lambda c: c.fii(["NSE_EQ|CASH"])),
        ("market/dii", lambda c: c.dii(["NSE_EQ|CASH"])),
    ]
    with UpstoxClient(_token()) as c:
        for name, fn in checks:
            try:
                r = fn(c)
                ok = r not in (None, {}, [])
                print(f"  {'✓' if ok else '·'} {name}: {'entitled' if ok else 'reachable (empty)'}")
            except TokenExpiredError:
                print(f"  ✗ {name}: TOKEN EXPIRED"); return 1
            except NotEntitledError:
                print(f"  ✗ {name}: NOT ENTITLED (403)")
            except UpstoxError as e:
                print(f"  ? {name}: {e}")
    return 0


def cmd_one(args) -> int:
    resolver = Resolver(DATA_DIR, last_trading_date())
    inst = resolver.resolve(args.symbol)
    if not inst:
        raise SystemExit(f"Could not resolve {args.symbol!r}.")
    date = last_trading_date()
    with UpstoxClient(_token()) as c:
        fund = build_fundamentals(c, inst)
        opt = build_options(c, inst.instrument_key, date) if inst.has_options else None
    print(f"{inst.symbol} ({inst.name}) ISIN {inst.isin}  F&O={inst.has_options}")
    print(f"  ratios: {fund['ratios']}")
    print(f"  income cats: {[r.get('category') for r in fund['income_statement'].get('income_statement', [])]}")
    print(f"  holdings: {len(fund['share_holdings'])}  actions: {len(fund['corporate_actions'])}  peers: {len(fund['competitors'])}")
    if opt:
        print(f"  options: PCR={opt['pcr']} max_pain={opt['max_pain']} spot={opt['spot']} strikes={len(opt['oi_by_strike'])} expiry={opt['expiry']}")
    if fund["notes"]:
        print(f"  notes: {fund['notes']}")
    return 0


def cmd_sync(args) -> int:
    symbols = _dashboard_symbols()
    if args.limit:
        symbols = symbols[:args.limit]
    with UpstoxClient(_token()) as c:
        payload = sync(c, symbols, OUT, DATA_DIR,
                       force_fundamentals=args.fundamentals,
                       fund_max_age_days=args.max_age_days)
    print(f"\nDONE — {payload['counts']}")
    if payload["unresolved"]:
        print(f"unresolved: {payload['unresolved']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="upstox_sync", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("login"); sp.add_argument("arg", nargs="?"); sp.set_defaults(func=cmd_login)
    sub.add_parser("probe").set_defaults(func=cmd_probe)
    sp = sub.add_parser("one"); sp.add_argument("symbol"); sp.set_defaults(func=cmd_one)
    sp = sub.add_parser("sync")
    sp.add_argument("--fundamentals", action="store_true", help="force-refresh fundamentals")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--max-age-days", type=int, default=7)
    sp.set_defaults(func=cmd_sync)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
