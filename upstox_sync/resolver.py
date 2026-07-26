"""Map dashboard symbols → Upstox ISIN + instrument_key, and detect F&O names.

The dashboard uses Yahoo-style symbols (``RELIANCE.NS``, ``M&M.NS``). Upstox's NSE
instrument master carries ``trading_symbol``, ``isin`` and ``instrument_key`` for
equities, and — via its NSE_FO rows' ``underlying_key`` — the set of underlyings that
have listed options. One daily-cached download bridges symbol ⇄ ISIN ⇄ key and yields
the F&O universe.
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

import requests

NSE_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
_HEADERS = {"User-Agent": "weinstein-upstox-sync/0.1"}

# Yahoo symbol → Upstox trading_symbol, where they differ (extend as needed).
ALIASES: dict[str, str] = {
    "TATAMOTORS": "TMCV",   # Tata Motors demerged; main listing is now TMCV
}


@dataclass(frozen=True)
class Instrument:
    symbol: str            # Upstox trading symbol
    name: str
    isin: str
    instrument_key: str
    has_options: bool


class Resolver:
    def __init__(self, data_dir: str | Path, day: str) -> None:
        self.data_dir = Path(data_dir)
        self.day = day
        self._by_symbol: dict[str, Instrument] = {}
        self._by_isin: dict[str, Instrument] = {}
        self._fno_keys: set[str] = set()
        self._loaded = False

    def _cache(self) -> Path:
        return self.data_dir / f"nse_master_{self.day}.json.gz"

    def _load(self) -> None:
        if self._loaded:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        cache = self._cache()
        if not cache.exists():
            for old in self.data_dir.glob("nse_master_*.json.gz"):
                try:
                    old.unlink()
                except OSError:
                    pass
            r = requests.get(NSE_MASTER_URL, headers=_HEADERS, timeout=120)
            r.raise_for_status()
            cache.write_bytes(r.content)
        with gzip.open(cache, "rt", encoding="utf-8") as fh:
            rows = json.load(fh)

        equities: list[dict] = []
        for inst in rows:
            seg = inst.get("segment") or ""
            itype = inst.get("instrument_type") or ""
            if seg == "NSE_FO" and itype in ("CE", "PE", "FUT"):
                uk = inst.get("underlying_key") or inst.get("asset_key")
                if uk:
                    self._fno_keys.add(uk)
            elif seg == "NSE_EQ" or itype in ("EQ", "EQUITY"):
                equities.append(inst)

        for inst in equities:
            isin = inst.get("isin") or ""
            key = inst.get("instrument_key") or ""
            sym = inst.get("trading_symbol") or inst.get("tradingsymbol") or ""
            if not (isin and key and sym):
                continue
            rec = Instrument(symbol=sym, name=inst.get("name") or sym, isin=isin,
                             instrument_key=key, has_options=key in self._fno_keys)
            self._by_symbol.setdefault(sym, rec)
            self._by_isin.setdefault(isin, rec)
        # has_options may have been set before the FO set was complete; recompute.
        self._by_symbol = {s: Instrument(r.symbol, r.name, r.isin, r.instrument_key,
                                         r.instrument_key in self._fno_keys)
                           for s, r in self._by_symbol.items()}
        self._by_isin = {i: self._by_symbol.get(r.symbol, r) for i, r in self._by_isin.items()}
        self._loaded = True

    @staticmethod
    def _norm(dashboard_symbol: str) -> str:
        """RELIANCE.NS -> RELIANCE ; apply alias."""
        s = dashboard_symbol.strip().upper()
        if s.endswith(".NS"):
            s = s[:-3]
        return ALIASES.get(s, s)

    def resolve(self, dashboard_symbol: str) -> Instrument | None:
        self._load()
        return self._by_symbol.get(self._norm(dashboard_symbol))

    @property
    def fno_underlyings(self) -> set[str]:
        self._load()
        return self._fno_keys
