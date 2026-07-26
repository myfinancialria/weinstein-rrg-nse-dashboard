"""Override each stock's headline ratios with Upstox values.

Imported by ``build_dashboard_data.py`` just before it writes ``dashboard_data.json``.
Upstox is authoritative for the ratios it provides; fields it does not expose (EPS,
D/E, FCF) are left as-is from the existing source. Safe no-op if the Upstox file is
absent (e.g. a first run before the VM has committed it).

Operates on the repo's pandas DataFrames (``stocks`` etc.), keyed by ``display_symbol``
(falling back to ``symbol`` with the ``.NS`` suffix stripped).
"""
from __future__ import annotations

import json
from pathlib import Path

# Upstox compact-ratio key -> stock column(s) to overwrite.
FIELD_MAP = {
    "pe_ratio": ("pe_ratio", "pe"),
    "pb_ratio": ("pb_ratio",),
    "roe_pct": ("roe_pct",),
    "roce": ("roce",),
    "net_margin_pct": ("net_margin_pct",),
    "operating_margin_pct": ("operating_margin_pct",),
    "promoter_or_insider_holding_pct": ("promoter_or_insider_holding_pct",),
}
_NEW_COLS = ("fundamentals_source", "has_options")


def load_fundamentals(upstox_data_path) -> dict:
    p = Path(upstox_data_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()).get("fundamentals", {}) or {}
    except Exception:
        return {}


def _key_of(row) -> str:
    disp = row.get("display_symbol")
    if disp:
        return str(disp)
    return str(row.get("symbol", "")).replace(".NS", "")


def apply_upstox_ratios_df(df, funds: dict) -> int:
    """Mutate a stocks DataFrame in place. ``funds`` is the fundamentals map.

    Returns the number of rows updated. Imports pandas lazily so this module has
    no hard dependency for the list-based callers.
    """
    if df is None or not funds or len(df) == 0:
        return 0
    for col in _NEW_COLS:
        if col not in df.columns:
            df[col] = None
    updated = 0
    for idx in df.index:
        row = df.loc[idx]
        entry = funds.get(_key_of(row))
        if not entry:
            continue
        ratios = entry.get("ratios", {}) or {}
        touched = False
        for rk, fields in FIELD_MAP.items():
            val = ratios.get(rk)
            if val is not None:
                for f in fields:
                    df.at[idx, f] = val
                touched = True
        if touched:
            df.at[idx, "fundamentals_source"] = "upstox"
            df.at[idx, "has_options"] = bool(entry.get("has_options", False))
            updated += 1
    return updated
