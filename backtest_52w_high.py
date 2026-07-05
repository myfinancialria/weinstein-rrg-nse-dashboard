"""52-week high breakout backtest.

Strategy (weekly candles):
  * Entry: buy-stop at the prior 52-week high. A trade fills the week the
    weekly high takes out that level (a fresh 52-week high breakout).
  * Stop-loss / exit: the 52-week moving average of weekly close. The stop
    sits below the 52W MA and trails up with it. The position is held until a
    weekly close prints below the 52W MA, then it is exited on that close.
  * There is no fixed profit target - the position rides the trend until the
    52W MA exit is triggered ("hold till the exit criteria is met").
  * Risk Rs 10,000 per stock. Quantity = risk / (entry - 52W MA at entry).

The backtest reports the last 6 completed years of signals. Prices are pulled
with ~2 extra years of history so the 52-week MA and 52-week high are already
warmed up at the start of the reporting window.
"""

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
OUT_JSON = DASHBOARD_DIR / "backtest_52w_high.json"

BENCHMARK_NOT_USED = None
RISK_PER_TRADE = 10_000
HIGH_WINDOW = 52          # 52-week breakout lookback
MA_WINDOW = 52            # 52-week moving average (stop / exit reference)
REPORT_WEEKS = 6 * 52     # report the last 6 years of weekly signals


def download_prices(symbols: list[str]) -> dict[str, pd.DataFrame]:
    data = yf.download(
        sorted(set(symbols)),
        period="8y",
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


def weekly_ohlc(frame: pd.DataFrame) -> pd.DataFrame:
    weekly = pd.DataFrame(
        {
            "open": frame["open"].resample("W-FRI").first(),
            "high": frame["high"].resample("W-FRI").max(),
            "low": frame["low"].resample("W-FRI").min(),
            "close": frame["close"].resample("W-FRI").last(),
        }
    ).dropna()
    if weekly.empty:
        return weekly
    # Prior 52-week high (breakout trigger, excludes the current week) and the
    # trailing 52-week moving average used for the stop / exit.
    weekly["high_52w"] = weekly["high"].rolling(HIGH_WINDOW).max().shift(1)
    weekly["ma_52w"] = weekly["close"].rolling(MA_WINDOW).mean()
    return weekly


def backtest_symbol(symbol: str, name: str, industry: str, frame: pd.DataFrame) -> list[dict]:
    weekly = weekly_ohlc(frame)
    if weekly.empty:
        return []
    weekly = weekly.dropna(subset=["high_52w", "ma_52w"])
    if weekly.empty:
        return []
    weekly = weekly.tail(REPORT_WEEKS)

    trades: list[dict] = []
    in_trade = False
    entry_date = None
    entry_price = 0.0
    entry_ma = 0.0
    breakout_level = 0.0
    qty = 0
    risk_per_share = 0.0

    for idx, row in weekly.iterrows():
        high = float(row["high"])
        close = float(row["close"])
        ma = float(row["ma_52w"])
        prior_high = float(row["high_52w"])

        if not in_trade:
            # Fresh 52-week high: this week's high takes out the prior 52W high.
            if high >= prior_high and prior_high > 0:
                fill = max(prior_high, 0.0)
                risk = fill - ma
                if risk > 0:
                    size = int(RISK_PER_TRADE // risk)
                    if size >= 1:
                        in_trade = True
                        entry_date = idx
                        entry_price = fill
                        entry_ma = ma
                        breakout_level = prior_high
                        risk_per_share = risk
                        qty = size
            continue

        # In a trade: exit on a weekly close below the trailing 52-week MA.
        if close < ma:
            exit_price = close
            pnl = qty * (exit_price - entry_price)
            deployed = qty * entry_price
            trades.append(
                _trade_record(
                    symbol, name, industry, entry_date, entry_price, entry_ma,
                    breakout_level, risk_per_share, qty, idx, exit_price, ma,
                    pnl, deployed, "Close below 52-week MA",
                )
            )
            in_trade = False

    if in_trade:
        last_idx = weekly.index[-1]
        last_close = float(weekly.iloc[-1]["close"])
        last_ma = float(weekly.iloc[-1]["ma_52w"])
        pnl = qty * (last_close - entry_price)
        deployed = qty * entry_price
        trades.append(
            _trade_record(
                symbol, name, industry, entry_date, entry_price, entry_ma,
                breakout_level, risk_per_share, qty, None, last_close, last_ma,
                pnl, deployed, "Open",
            )
        )
    return trades


def _trade_record(symbol, name, industry, entry_date, entry_price, entry_ma,
                  breakout_level, risk_per_share, qty, exit_idx, exit_price,
                  exit_ma, pnl, deployed, reason) -> dict:
    open_trade = exit_idx is None
    end_idx = exit_idx if not open_trade else entry_date
    holding_days = int((exit_idx - entry_date).days) if not open_trade else None
    return {
        "symbol": symbol.replace(".NS", ""),
        "yahoo_symbol": symbol,
        "name": name,
        "industry": industry,
        "entry_date": entry_date.date().isoformat(),
        "entry_price": round(entry_price, 2),
        "breakout_52w_high": round(breakout_level, 2),
        "initial_sl_52w_ma": round(entry_ma, 2),
        "entry_52w_ma": round(entry_ma, 2),
        "risk_per_share": round(risk_per_share, 2),
        "quantity": qty,
        "risk_per_trade": RISK_PER_TRADE,
        "exit_date": exit_idx.date().isoformat() if not open_trade else "",
        "exit_price": round(exit_price, 2),
        "exit_52w_ma": round(exit_ma, 2),
        "holding_days": holding_days,
        "pnl": round(pnl, 2),
        "return_pct_on_deployed": round((pnl / deployed) * 100, 2) if deployed else None,
        "r_multiple": round(pnl / RISK_PER_TRADE, 2),
        "exit_reason": reason,
    }


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
        curve.append(
            {
                "date": date_value,
                "equity": round(equity, 2),
                "drawdown": round(equity - peak, 2),
            }
        )
    return curve


def monthly_pnl(trades: list[dict]) -> list[dict]:
    rows: dict[str, dict] = {}
    for trade in trades:
        date_value = trade["exit_date"] or date.today().isoformat()
        month = date_value[:7]
        row = rows.setdefault(month, {"month": month, "trades": 0, "pnl": 0.0, "wins": 0, "losses": 0})
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
            {"month": month, "entries": 0, "capital_deployed": 0.0, "avg_capital_per_trade": 0.0, "max_single_trade_capital": 0.0},
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


def summarize(trades: list[dict]) -> dict:
    closed = [trade for trade in trades if trade["exit_reason"] != "Open"]
    wins = [trade for trade in closed if trade["pnl"] > 0]
    total_pnl = sum(trade["pnl"] for trade in trades)
    curve = equity_points(trades)
    max_drawdown = min((point["drawdown"] for point in curve), default=0)
    holding = [trade["holding_days"] for trade in closed if trade["holding_days"] is not None]
    return {
        "generated_at": date.today().isoformat(),
        "timeframe": "weekly",
        "lookback_years": 6,
        "risk_per_trade": RISK_PER_TRADE,
        "trade_count": len(trades),
        "closed_trades": len(closed),
        "open_trades": len(trades) - len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else None,
        "total_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_drawdown, 2),
        "avg_r_multiple": round(np.mean([trade["r_multiple"] for trade in closed]), 2) if closed else None,
        "avg_holding_days": round(float(np.mean(holding)), 0) if holding else None,
        "best_trade": max((trade["pnl"] for trade in trades), default=0),
        "worst_trade": min((trade["pnl"] for trade in trades), default=0),
        "rules": (
            "6-year weekly backtest. Entry: breakout of the prior 52-week high "
            "(buy-stop filled the week the weekly high takes out that level). "
            "Stop-loss sits at the 52-week moving average and trails up with it. "
            "Exit: weekly close below the 52-week MA - the position is held until "
            "then, with no fixed profit target. Risk Rs 10,000 per stock."
        ),
    }


def main() -> None:
    stocks = pd.read_csv(STOCK_CSV)
    stocks = stocks.dropna(subset=["symbol"]).drop_duplicates("symbol")
    symbols = stocks["symbol"].tolist()
    prices = download_prices(symbols)

    all_trades: list[dict] = []
    for row in stocks.itertuples(index=False):
        frame = prices.get(row.symbol)
        if frame is None or frame.empty:
            continue
        all_trades.extend(backtest_symbol(row.symbol, row.name, row.parent, frame))

    all_trades = sorted(all_trades, key=lambda trade: (trade["entry_date"], trade["symbol"]), reverse=True)
    payload = {
        "summary": summarize(all_trades),
        "trades": all_trades,
        "monthlyPnl": monthly_pnl(all_trades),
        "monthlyCapital": monthly_capital_deployed(all_trades),
        "equityCurve": equity_points(all_trades),
    }
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(OUT_JSON)


if __name__ == "__main__":
    main()
