from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf


BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
DASHBOARD_DIR = BASE_DIR / "dashboard"
STOCK_CSV = REPORTS_DIR / "screener_industry_stock_rankings_2026-06-28.csv"
OUT_JSON = DASHBOARD_DIR / "backtest_results.json"

BENCHMARK = "^NSEI"
RISK_PER_TRADE = 10_000
RATIO_WINDOW = 50
MOMENTUM_WINDOW = 10
SWING_LOOKBACK = 20
TIMEFRAMES = {
    "daily": {"bars": 1260, "resample": None},
    "weekly": {"bars": 260, "resample": "W-FRI"},
}


def download_prices(symbols: list[str]) -> dict[str, pd.DataFrame]:
    data = yf.download(
        sorted(set(symbols)),
        period="7y",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=True,
        group_by="column",
    )
    if data.empty:
        raise RuntimeError("No Yahoo Finance data returned.")
    result: dict[str, pd.DataFrame] = {}
    fields = ["Open", "High", "Low", "Close"]
    for symbol in symbols:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                frame = pd.DataFrame({field.lower(): data[(field, symbol)] for field in fields})
            else:
                frame = data[fields].rename(columns={field: field.lower() for field in fields})
            frame.index = pd.to_datetime(frame.index).tz_localize(None)
            result[symbol] = frame.dropna()
        except Exception:
            result[symbol] = pd.DataFrame()
    return result


def resample_ohlc(frame: pd.DataFrame, rule: str | None) -> pd.DataFrame:
    if not rule:
        return frame
    return pd.DataFrame(
        {
            "open": frame["open"].resample(rule).first(),
            "high": frame["high"].resample(rule).max(),
            "low": frame["low"].resample(rule).min(),
            "close": frame["close"].resample(rule).last(),
        }
    ).dropna()


def rrg_quadrants(asset_ohlc: pd.DataFrame, benchmark_ohlc: pd.DataFrame) -> pd.DataFrame:
    joined = asset_ohlc[["open", "high", "low", "close"]].join(
        benchmark_ohlc[["close"]].rename(columns={"close": "benchmark"}),
        how="inner",
    ).dropna()
    joined = joined.rename(columns={"close": "asset"})
    joined["rs"] = joined["asset"] / joined["benchmark"]
    joined["rs_ratio"] = 100 + (
        (joined["rs"] - joined["rs"].rolling(RATIO_WINDOW).mean())
        / joined["rs"].rolling(RATIO_WINDOW).std(ddof=0)
    )
    joined["rs_momentum"] = 100 + (
        (joined["rs_ratio"] - joined["rs_ratio"].shift(MOMENTUM_WINDOW))
        / joined["rs_ratio"].rolling(MOMENTUM_WINDOW).std(ddof=0)
    )
    conditions = [
        (joined["rs_ratio"] >= 100) & (joined["rs_momentum"] >= 100),
        (joined["rs_ratio"] >= 100) & (joined["rs_momentum"] < 100),
        (joined["rs_ratio"] < 100) & (joined["rs_momentum"] < 100),
    ]
    choices = ["leading", "weakening", "lagging"]
    joined["quadrant"] = np.select(conditions, choices, default="improving")
    joined["dma_50"] = joined["asset"].rolling(50).mean()
    joined["dma_200"] = joined["asset"].rolling(200).mean()
    return joined.dropna()


def previous_swing_low(frame: pd.DataFrame, position: int) -> float | None:
    if position < 2:
        return None
    start = max(0, position - SWING_LOOKBACK)
    prior = frame.iloc[start:position]
    if prior.empty:
        return None
    return float(prior["low"].min())


def backtest_symbol(
    symbol: str,
    name: str,
    industry: str,
    prices: dict[str, pd.DataFrame],
    timeframe: str,
    bars: int,
) -> list[dict]:
    if symbol not in prices or BENCHMARK not in prices:
        return []
    asset_prices = resample_ohlc(prices[symbol], TIMEFRAMES[timeframe]["resample"])
    benchmark_prices = resample_ohlc(prices[BENCHMARK], TIMEFRAMES[timeframe]["resample"])
    frame = rrg_quadrants(asset_prices, benchmark_prices)
    if frame.empty:
        return []
    frame = frame.tail(bars)
    trades: list[dict] = []
    in_trade = False
    entry_date = None
    entry_price = 0.0
    signal_date = None
    signal_high = 0.0
    qty = 0
    half_qty = 0
    remaining_qty = 0
    first_target_done = False
    target_date = None
    target_price = None
    realized_first = 0.0
    risk_per_share = 0.0

    previous_quadrant = None
    pending_entry: dict | None = None
    rows = list(frame.iterrows())
    for position, (idx, row) in enumerate(rows):
        price = float(row["asset"])
        dma_50 = float(row["dma_50"])
        dma_200 = float(row["dma_200"])
        quadrant = row["quadrant"]
        signal = previous_quadrant == "improving" and quadrant == "leading"

        if not in_trade and pending_entry is not None:
            if float(row["high"]) >= pending_entry["signal_high"]:
                entry_price = pending_entry["signal_high"]
                risk_per_share = entry_price - pending_entry["swing_low"]
                if risk_per_share > 0:
                    qty = int(RISK_PER_TRADE // risk_per_share)
                    if qty > 1:
                        in_trade = True
                        entry_date = idx
                        signal_date = pending_entry["signal_date"]
                        signal_high = pending_entry["signal_high"]
                        half_qty = qty // 2
                        remaining_qty = qty
                        first_target_done = False
                        target_date = None
                        target_price = None
                        realized_first = 0.0
                pending_entry = None
            elif price < float(row["dma_50"]):
                pending_entry = None

        trend_filter = price > dma_50 and price > dma_200 and dma_50 > dma_200
        if not in_trade and pending_entry is None and signal and trend_filter:
            swing_low = previous_swing_low(frame, position)
            if swing_low is not None and float(row["high"]) > swing_low:
                pending_entry = {
                    "signal_date": idx,
                    "signal_high": float(row["high"]),
                    "swing_low": swing_low,
                }

        elif in_trade:
            if (not first_target_done) and float(row["high"]) >= entry_price * 1.3:
                target_date = idx
                target_price = entry_price * 1.3
                realized_first = half_qty * (target_price - entry_price)
                remaining_qty -= half_qty
                first_target_done = True

            if price < dma_50:
                exit_price = price
                final_pnl = remaining_qty * (exit_price - entry_price)
                total_pnl = realized_first + final_pnl
                deployed = qty * entry_price
                trades.append(
                    {
                        "symbol": symbol.replace(".NS", ""),
                        "timeframe": timeframe,
                        "yahoo_symbol": symbol,
                        "name": name,
                        "industry": industry,
                        "signal_date": signal_date.date().isoformat() if signal_date is not None else "",
                        "signal_candle_high": round(signal_high, 2),
                        "entry_date": entry_date.date().isoformat(),
                        "entry_price": round(entry_price, 2),
                        "initial_sl_swing_low": round(entry_price - risk_per_share, 2),
                        "entry_filter_50dma": round(dma_50, 2),
                        "entry_filter_200dma": round(dma_200, 2),
                        "exit_filter_50dma": round(dma_50, 2),
                        "quantity": qty,
                        "risk_per_trade": RISK_PER_TRADE,
                        "target_30_hit": first_target_done,
                        "target_date": target_date.date().isoformat() if target_date is not None else "",
                        "target_price": round(target_price, 2) if target_price is not None else None,
                        "exit_date": idx.date().isoformat(),
                        "exit_price": round(exit_price, 2),
                        "holding_days": int((idx - entry_date).days),
                        "pnl": round(total_pnl, 2),
                        "return_pct_on_deployed": round((total_pnl / deployed) * 100, 2) if deployed else None,
                        "r_multiple": round(total_pnl / RISK_PER_TRADE, 2),
                        "exit_reason": "Close below 50 DMA",
                    }
                )
                in_trade = False

        previous_quadrant = quadrant

    if in_trade:
        last_idx = frame.index[-1]
        last_price = float(frame.iloc[-1]["asset"])
        if (not first_target_done) and last_price >= entry_price * 1.3:
            realized_first = half_qty * (last_price - entry_price)
            remaining_qty -= half_qty
            first_target_done = True
            target_date = last_idx
            target_price = last_price
        total_pnl = realized_first + remaining_qty * (last_price - entry_price)
        deployed = qty * entry_price
        trades.append(
            {
                "symbol": symbol.replace(".NS", ""),
                "timeframe": timeframe,
                "yahoo_symbol": symbol,
                "name": name,
                "industry": industry,
                "signal_date": signal_date.date().isoformat() if signal_date is not None else "",
                "signal_candle_high": round(signal_high, 2),
                "entry_date": entry_date.date().isoformat(),
                "entry_price": round(entry_price, 2),
                "initial_sl_swing_low": round(entry_price - risk_per_share, 2),
                "entry_filter_50dma": None,
                "entry_filter_200dma": None,
                "exit_filter_50dma": round(float(frame.iloc[-1]["dma_50"]), 2),
                "quantity": qty,
                "risk_per_trade": RISK_PER_TRADE,
                "target_30_hit": first_target_done,
                "target_date": target_date.date().isoformat() if target_date is not None else "",
                "target_price": round(target_price, 2) if target_price is not None else None,
                "exit_date": "",
                "exit_price": round(last_price, 2),
                "holding_days": int((last_idx - entry_date).days),
                "pnl": round(total_pnl, 2),
                "return_pct_on_deployed": round((total_pnl / deployed) * 100, 2) if deployed else None,
                "r_multiple": round(total_pnl / RISK_PER_TRADE, 2),
                "exit_reason": "Open",
            }
        )
    return trades


def equity_points(trades: list[dict]) -> list[dict]:
    events = []
    for trade in trades:
        date_value = trade["exit_date"] or date.today().isoformat()
        events.append((date_value, trade["pnl"]))
    if not events:
        return []
    curve = []
    equity = 0.0
    peak = 0.0
    for date_value, pnl in sorted(events):
        equity += pnl
        peak = max(peak, equity)
        drawdown = equity - peak
        curve.append(
            {
                "date": date_value,
                "equity": round(equity, 2),
                "drawdown": round(drawdown, 2),
            }
        )
    return curve


def monthly_pnl(trades: list[dict]) -> list[dict]:
    rows: dict[str, dict] = {}
    for trade in trades:
        date_value = trade["exit_date"] or date.today().isoformat()
        month = date_value[:7]
        row = rows.setdefault(
            month,
            {
                "month": month,
                "trades": 0,
                "pnl": 0.0,
                "wins": 0,
                "losses": 0,
            },
        )
        row["trades"] += 1
        row["pnl"] += trade["pnl"]
        if trade["pnl"] > 0:
            row["wins"] += 1
        else:
            row["losses"] += 1
    return [
        {
            **row,
            "pnl": round(row["pnl"], 2),
            "win_rate_pct": round(row["wins"] / row["trades"] * 100, 2) if row["trades"] else None,
        }
        for row in sorted(rows.values(), key=lambda item: item["month"])
    ]


def monthly_capital_deployed(trades: list[dict]) -> list[dict]:
    rows: dict[str, dict] = {}
    for trade in trades:
        month = trade["entry_date"][:7]
        deployed = trade["entry_price"] * trade["quantity"]
        row = rows.setdefault(
            month,
            {
                "month": month,
                "entries": 0,
                "capital_deployed": 0.0,
                "avg_capital_per_trade": 0.0,
                "max_single_trade_capital": 0.0,
            },
        )
        row["entries"] += 1
        row["capital_deployed"] += deployed
        row["max_single_trade_capital"] = max(row["max_single_trade_capital"], deployed)
    for row in rows.values():
        row["avg_capital_per_trade"] = row["capital_deployed"] / row["entries"] if row["entries"] else 0
        row["capital_deployed"] = round(row["capital_deployed"], 2)
        row["avg_capital_per_trade"] = round(row["avg_capital_per_trade"], 2)
        row["max_single_trade_capital"] = round(row["max_single_trade_capital"], 2)
    return sorted(rows.values(), key=lambda item: item["month"])


def summarize(trades: list[dict], timeframe: str) -> dict:
    closed = [trade for trade in trades if trade["exit_reason"] != "Open"]
    wins = [trade for trade in closed if trade["pnl"] > 0]
    losses = [trade for trade in closed if trade["pnl"] <= 0]
    total_pnl = sum(trade["pnl"] for trade in trades)
    curve = equity_points(trades)
    max_drawdown = min((point["drawdown"] for point in curve), default=0)
    return {
        "generated_at": date.today().isoformat(),
        "timeframe": timeframe,
        "risk_per_trade": RISK_PER_TRADE,
        "trade_count": len(trades),
        "closed_trades": len(closed),
        "open_trades": len(trades) - len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else None,
        "total_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_drawdown, 2),
        "avg_r_multiple": round(np.mean([trade["r_multiple"] for trade in closed]), 2) if closed else None,
        "best_trade": max((trade["pnl"] for trade in trades), default=0),
        "worst_trade": min((trade["pnl"] for trade in trades), default=0),
        "target_30_hit_count": sum(1 for trade in trades if trade["target_30_hit"]),
        "rules": "5-year backtest. Signal: RRG improving -> leading. Entry: signal candle high breakout only when close is above 50 MA and 200 MA, with 50 MA > 200 MA. Initial SL: previous 20-bar swing low. Risk: Rs 10,000 per stock. Book half at +30%. Exit remaining on close below 50 MA.",
    }


def main() -> None:
    stocks = pd.read_csv(STOCK_CSV)
    stocks = stocks.dropna(subset=["symbol"])
    symbols = stocks["symbol"].drop_duplicates().tolist()
    prices = download_prices(symbols + [BENCHMARK])
    results = {}
    for timeframe, settings in TIMEFRAMES.items():
        all_trades: list[dict] = []
        for row in stocks.drop_duplicates("symbol").itertuples(index=False):
            all_trades.extend(
                backtest_symbol(
                    row.symbol,
                    row.name,
                    row.parent,
                    prices,
                    timeframe,
                    settings["bars"],
                )
            )
        all_trades = sorted(all_trades, key=lambda trade: (trade["entry_date"], trade["symbol"]), reverse=True)
        results[timeframe] = {
            "summary": summarize(all_trades, timeframe),
            "trades": all_trades,
            "monthlyPnl": monthly_pnl(all_trades),
            "monthlyCapital": monthly_capital_deployed(all_trades),
            "equityCurve": equity_points(all_trades),
        }
    payload = {
        "daily": results["daily"],
        "weekly": results["weekly"],
    }
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(OUT_JSON)


if __name__ == "__main__":
    main()
