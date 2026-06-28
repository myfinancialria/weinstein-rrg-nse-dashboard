from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class UniverseItem:
    level: str
    name: str
    symbol: str
    parent: str


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    config["_base_dir"] = path.parent
    return config


def load_universe(path: Path) -> list[UniverseItem]:
    df = pd.read_csv(path).fillna("")
    required = {"level", "name", "symbol", "parent"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Universe file is missing columns: {', '.join(sorted(missing))}")
    return [
        UniverseItem(
            level=str(row.level).strip().lower(),
            name=str(row.name).strip(),
            symbol=str(row.symbol).strip(),
            parent=str(row.parent).strip(),
        )
        for row in df.itertuples(index=False)
        if str(row.symbol).strip()
    ]


class FyersHistoryClient:
    def __init__(self, client_id: str, access_token: str) -> None:
        try:
            from fyers_apiv3 import fyersModel
        except ImportError as exc:
            raise RuntimeError(
                "Install dependencies first: python3 -m pip install -r requirements.txt"
            ) from exc
        self._client = fyersModel.FyersModel(
            client_id=client_id,
            token=access_token,
            log_path=str(Path.cwd()),
        )

    def history(
        self,
        symbol: str,
        start: date,
        end: date,
        resolution: str = "D",
    ) -> pd.DataFrame:
        payload = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": start.isoformat(),
            "range_to": end.isoformat(),
            "cont_flag": "1",
        }
        response = self._client.history(data=payload)
        if not isinstance(response, dict) or response.get("s") != "ok":
            raise RuntimeError(f"FYERS history failed for {symbol}: {response}")
        candles = response.get("candles", [])
        if not candles:
            raise RuntimeError(f"FYERS returned no candles for {symbol}")
        df = pd.DataFrame(
            candles,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["date"] = pd.to_datetime(df["timestamp"], unit="s").dt.tz_localize(None)
        df = df.drop(columns=["timestamp"]).set_index("date").sort_index()
        return df.astype(float)


class YahooHistoryClient:
    def __init__(self) -> None:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError(
                "Install dependencies first: python3 -m pip install -r requirements.txt"
            ) from exc
        self._yf = yf

    def history(
        self,
        symbol: str,
        start: date,
        end: date,
        resolution: str = "1d",
    ) -> pd.DataFrame:
        df = self._yf.download(
            symbol,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            interval=resolution,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if df.empty:
            raise RuntimeError(f"Yahoo Finance returned no candles for {symbol}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Adj Close": "adj_close",
                "Volume": "volume",
            }
        )
        required = ["open", "high", "low", "close", "volume"]
        missing = [column for column in required if column not in df.columns]
        if missing:
            raise RuntimeError(f"Yahoo Finance data for {symbol} is missing: {', '.join(missing)}")
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df[required].dropna(subset=["close"]).astype(float)


def create_history_client(config: dict[str, Any]) -> Any:
    provider = str(config.get("data_provider", "fyers")).strip().lower()
    if provider in {"yahoo", "yfinance"}:
        return YahooHistoryClient()
    if provider == "fyers":
        load_dotenv(config["_base_dir"] / ".env")
        client_id = os.getenv("FYERS_CLIENT_ID")
        access_token = os.getenv("FYERS_ACCESS_TOKEN")
        if not client_id or not access_token:
            raise RuntimeError("Set FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN in .env or your shell.")
        return FyersHistoryClient(client_id=client_id, access_token=access_token)
    raise ValueError(f"Unsupported data_provider: {provider}")


def weekly_bars(daily: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": daily["open"].resample("W-FRI").first(),
            "high": daily["high"].resample("W-FRI").max(),
            "low": daily["low"].resample("W-FRI").min(),
            "close": daily["close"].resample("W-FRI").last(),
            "volume": daily["volume"].resample("W-FRI").sum(),
        }
    ).dropna()


def pct_slope(series: pd.Series, periods: int = 4) -> float:
    current = series.iloc[-1]
    previous = series.iloc[-periods - 1] if len(series) > periods else np.nan
    if pd.isna(previous) or previous == 0:
        return np.nan
    return float((current / previous - 1) * 100)


def analyze_weinstein(
    daily: pd.DataFrame,
    benchmark_daily: pd.DataFrame,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    settings = cfg["weinstein"]
    weekly = weekly_bars(daily)
    benchmark_weekly = weekly_bars(benchmark_daily)
    joined = weekly[["close", "volume"]].join(
        benchmark_weekly[["close"]].rename(columns={"close": "benchmark_close"}),
        how="inner",
    )
    if len(joined) < settings["weekly_ma_weeks"] + 5:
        return {
            "stage": "insufficient_data",
            "stage2_score": 0,
            "close": np.nan,
            "weekly_ma": np.nan,
            "weekly_ma_slope_4w_pct": np.nan,
            "rs_vs_benchmark": np.nan,
            "checks": {},
        }

    ma_window = int(settings["weekly_ma_weeks"])
    fast_window = int(settings["fast_ma_weeks"])
    rs_window = int(settings["relative_strength_ma_weeks"])
    volume_window = int(settings["volume_ma_weeks"])

    joined["ma"] = joined["close"].rolling(ma_window).mean()
    joined["fast_ma"] = joined["close"].rolling(fast_window).mean()
    joined["rs"] = joined["close"] / joined["benchmark_close"]
    joined["rs_ma"] = joined["rs"].rolling(rs_window).mean()
    joined["volume_ma"] = joined["volume"].rolling(volume_window).mean()
    joined["high_52w"] = joined["close"].rolling(52, min_periods=20).max()
    latest = joined.iloc[-1]

    ma_slope = pct_slope(joined["ma"].dropna(), periods=4)
    checks = {
        "price_above_30w_ma": bool(latest["close"] > latest["ma"]),
        "30w_ma_rising": bool(ma_slope >= float(settings["min_ma_slope_pct"])),
        "price_above_10w_ma": bool(latest["close"] > latest["fast_ma"]),
        "relative_strength_positive": bool(latest["rs"] > latest["rs_ma"]),
        "near_52w_high": bool(
            latest["close"] >= latest["high_52w"] * float(settings["high_52w_proximity"])
        ),
    }
    if settings.get("require_volume_above_ma", False):
        checks["volume_above_ma"] = bool(latest["volume"] > latest["volume_ma"])

    score = int(sum(checks.values()))
    stage = "stage_2" if score >= len(checks) - 1 and checks["price_above_30w_ma"] and checks["30w_ma_rising"] else "not_stage_2"
    return {
        "stage": stage,
        "stage2_score": score,
        "close": round(float(latest["close"]), 2),
        "weekly_ma": round(float(latest["ma"]), 2),
        "weekly_ma_slope_4w_pct": round(float(ma_slope), 2),
        "rs_vs_benchmark": round(float(latest["rs"]), 5),
        "checks": checks,
    }


def analyze_rrg(
    daily: pd.DataFrame,
    benchmark_daily: pd.DataFrame,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    settings = cfg["rrg"]
    ratio_window = int(settings["ratio_window_days"])
    momentum_window = int(settings["momentum_window_days"])
    lookback = int(settings["lookback_days"])

    joined = daily[["close"]].rename(columns={"close": "asset"}).join(
        benchmark_daily[["close"]].rename(columns={"close": "benchmark"}),
        how="inner",
    )
    joined["rs"] = joined["asset"] / joined["benchmark"]
    joined["rs_ratio"] = 100 + (
        (joined["rs"] - joined["rs"].rolling(ratio_window).mean())
        / joined["rs"].rolling(ratio_window).std(ddof=0)
    )
    joined["rs_momentum"] = 100 + (
        (
            joined["rs_ratio"]
            - joined["rs_ratio"].shift(momentum_window)
        )
        / joined["rs_ratio"].rolling(momentum_window).std(ddof=0)
    )
    latest = joined.dropna().tail(lookback)
    if latest.empty:
        return {
            "rrg_quadrant": "insufficient_data",
            "rrg_quadrant_5d_ago": "insufficient_data",
            "rs_ratio": np.nan,
            "rs_momentum": np.nan,
            "rs_ratio_5d_delta": np.nan,
            "rs_momentum_5d_delta": np.nan,
            "moving_to_leading": False,
            "improving_toward_leading": False,
            "rrg_tail": "",
        }

    ratio_center = float(settings["ratio_center"])
    momentum_center = float(settings["momentum_center"])

    def quadrant_for(row: pd.Series) -> str:
        if row["rs_ratio"] >= ratio_center and row["rs_momentum"] >= momentum_center:
            return "leading"
        if row["rs_ratio"] >= ratio_center and row["rs_momentum"] < momentum_center:
            return "weakening"
        if row["rs_ratio"] < ratio_center and row["rs_momentum"] < momentum_center:
            return "lagging"
        return "improving"

    last = latest.iloc[-1]
    previous = latest.iloc[0]
    quadrant = quadrant_for(last)
    previous_quadrant = quadrant_for(previous)

    tail = "; ".join(
        f"{idx.date()} ratio={row.rs_ratio:.2f} momentum={row.rs_momentum:.2f}"
        for idx, row in latest.iterrows()
    )
    ratio_delta = latest["rs_ratio"].iloc[-1] - latest["rs_ratio"].iloc[0]
    momentum_delta = latest["rs_momentum"].iloc[-1] - latest["rs_momentum"].iloc[0]
    moving_to_leading = bool(
        quadrant == "leading"
        and previous_quadrant != "leading"
        and ratio_delta > 0
        and momentum_delta > 0
    )
    improving_toward_leading = bool(
        quadrant == "improving"
        and ratio_delta > 0
        and momentum_delta > 0
    )
    return {
        "rrg_quadrant": quadrant,
        "rrg_quadrant_5d_ago": previous_quadrant,
        "rs_ratio": round(float(last["rs_ratio"]), 2),
        "rs_momentum": round(float(last["rs_momentum"]), 2),
        "rs_ratio_5d_delta": round(float(ratio_delta), 2),
        "rs_momentum_5d_delta": round(float(momentum_delta), 2),
        "moving_to_leading": moving_to_leading,
        "improving_toward_leading": improving_toward_leading,
        "rrg_tail": tail,
    }


def error_row(item: UniverseItem, exc: Exception) -> dict[str, Any]:
    return {
        "level": item.level,
        "name": item.name,
        "symbol": item.symbol,
        "parent": item.parent,
        "stage": "data_error",
        "stage2_score": 0,
        "close": np.nan,
        "weekly_ma": np.nan,
        "weekly_ma_slope_4w_pct": np.nan,
        "rs_vs_benchmark": np.nan,
        "checks": {},
        "rrg_quadrant": "data_error",
        "rrg_quadrant_5d_ago": "data_error",
        "rs_ratio": np.nan,
        "rs_momentum": np.nan,
        "rs_ratio_5d_delta": np.nan,
        "rs_momentum_5d_delta": np.nan,
        "moving_to_leading": False,
        "improving_toward_leading": False,
        "rrg_tail": "",
        "error": str(exc),
    }


def build_report(
    config: dict[str, Any],
    universe: list[UniverseItem],
    output_dir: Path,
) -> tuple[Path, Path]:
    end = date.today()
    start = end - timedelta(days=int(config["history"]["lookback_days"]))
    client = create_history_client(config)
    benchmark_symbol = config["benchmark"]["symbol"]
    benchmark = client.history(
        benchmark_symbol,
        start=start,
        end=end,
        resolution=str(config["history"]["resolution"]),
    )

    rows: list[dict[str, Any]] = []
    for item in universe:
        try:
            daily = client.history(
                item.symbol,
                start=start,
                end=end,
                resolution=str(config["history"]["resolution"]),
            )
            rows.append(
                {
                    "level": item.level,
                    "name": item.name,
                    "symbol": item.symbol,
                    "parent": item.parent,
                    "error": "",
                    **analyze_weinstein(daily, benchmark, config),
                    **analyze_rrg(daily, benchmark, config),
                }
            )
        except Exception as exc:
            rows.append(error_row(item, exc))

    report = pd.DataFrame(rows)
    report["is_candidate"] = (
        (report["stage"] == "stage_2")
        & (report["rrg_quadrant"].isin(["leading", "improving"]))
    )
    report["sector_moving_to_leading"] = (
        (report["level"] == "sector")
        & (
            (report["moving_to_leading"])
            | (report["improving_toward_leading"])
            | (
                (report["rrg_quadrant"] == "leading")
                & (report["rs_ratio_5d_delta"] > 0)
                & (report["rs_momentum_5d_delta"] > 0)
            )
        )
    )
    report = report.sort_values(
        by=["is_candidate", "level", "stage2_score", "rs_ratio", "rs_momentum"],
        ascending=[False, True, False, False, False],
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    csv_path = output_dir / f"weinstein_rrg_screener_{stamp}.csv"
    md_path = output_dir / f"weinstein_rrg_screener_{stamp}.md"
    report.to_csv(csv_path, index=False)
    write_markdown_report(report, md_path, config)
    return csv_path, md_path


def write_markdown_report(report: pd.DataFrame, path: Path, config: dict[str, Any]) -> None:
    candidates = report[report["is_candidate"]]
    sector_moves = report[report["sector_moving_to_leading"]]
    lines = [
        f"# Weinstein Stage 2 + RRG Daily Screener ({date.today().isoformat()})",
        "",
        f"Benchmark: {config['benchmark']['name']} ({config['benchmark']['symbol']})",
        "",
        "## Candidates",
        "",
    ]
    if candidates.empty:
        lines.append("No Stage 2 instruments in Leading or Improving RRG quadrants today.")
    else:
        lines.append(
            candidates[
                [
                    "level",
                    "name",
                    "symbol",
                    "parent",
                    "stage2_score",
                    "rrg_quadrant",
                    "rs_ratio",
                    "rs_momentum",
                    "rs_ratio_5d_delta",
                    "rs_momentum_5d_delta",
                ]
            ].to_markdown(index=False)
        )
    lines.extend(["", "## Sectors Moving Toward Or Strengthening In Leading", ""])
    if sector_moves.empty:
        lines.append("No sector has a 5-day RRG tail moving into, toward, or strengthening in Leading.")
    else:
        lines.append(
            sector_moves[
                [
                    "name",
                    "symbol",
                    "stage",
                    "rrg_quadrant_5d_ago",
                    "rrg_quadrant",
                    "rs_ratio",
                    "rs_momentum",
                    "rs_ratio_5d_delta",
                    "rs_momentum_5d_delta",
                ]
            ].to_markdown(index=False)
        )
    lines.extend(
        [
            "",
            "## Full Screener",
            "",
            report[
                [
                    "level",
                    "name",
                    "symbol",
                    "parent",
                    "stage",
                    "stage2_score",
                    "weekly_ma_slope_4w_pct",
                    "rrg_quadrant_5d_ago",
                    "rrg_quadrant",
                    "rs_ratio",
                    "rs_momentum",
                    "error",
                ]
            ].to_markdown(index=False),
            "",
            "RRG values are normalized relative-strength estimates intended for screening, not a licensed JdK RS-Ratio clone.",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily Weinstein Stage 2 + RRG screener using FYERS history.")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML.")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    base_dir = config["_base_dir"]
    universe_path = (base_dir / config["universe_file"]).resolve()
    output_dir = (base_dir / config["output_dir"]).resolve()
    universe = load_universe(universe_path)
    csv_path, md_path = build_report(config, universe, output_dir)
    print(f"Wrote {csv_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
