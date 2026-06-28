from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf


BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
DASHBOARD_DATA = DASHBOARD_DIR / "dashboard_data.json"
BLOG_JSON = DASHBOARD_DIR / "market_blog.json"
TZ = ZoneInfo("Asia/Kolkata")


GLOBAL_SYMBOLS = {
    "S&P 500": "^GSPC",
    "Dow Jones": "^DJI",
    "Nasdaq": "^IXIC",
    "FTSE 100": "^FTSE",
    "Nikkei 225": "^N225",
    "Hang Seng": "^HSI",
    "Shanghai": "000001.SS",
    "USD/INR": "INR=X",
    "Brent Crude": "BZ=F",
    "Gold": "GC=F",
}


def read_env() -> dict[str, str]:
    env_path = BASE_DIR / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in {None, 0}:
        return None
    return round((current / previous - 1) * 100, 2)


def latest_change(symbol: str, period: str = "10d") -> tuple[float | None, float | None]:
    try:
        data = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=False)
    except Exception:
        return None, None
    if data.empty or "Close" not in data:
        return None, None
    closes = data["Close"]
    if isinstance(closes, pd.DataFrame):
        if closes.empty:
            return None, None
        closes = closes.iloc[:, 0]
    closes = closes.dropna()
    if len(closes) < 2:
        return None, None
    current = float(closes.iloc[-1])
    previous = float(closes.iloc[-2])
    return round(current, 2), pct_change(current, previous)


def load_nifty() -> pd.DataFrame:
    data = yf.download("^NSEI", period="2y", interval="1d", progress=False, auto_adjust=False)
    if isinstance(data.columns, pd.MultiIndex):
        data = data.xs("^NSEI", axis=1, level=1, drop_level=True)
    return data.dropna(subset=["Open", "High", "Low", "Close"]).copy()


def rsi(close: pd.Series, window: int = 14) -> float | None:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss.replace(0, math.nan)
    value = 100 - (100 / (1 + rs))
    latest = value.dropna()
    return round(float(latest.iloc[-1]), 2) if not latest.empty else None


def calculate_cpr(prev: pd.Series) -> dict[str, float]:
    high = float(prev["High"])
    low = float(prev["Low"])
    close = float(prev["Close"])
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = (2 * pivot) - bc
    cpr_low = min(bc, tc)
    cpr_high = max(bc, tc)
    return {
        "pivot": round(pivot, 2),
        "bc": round(cpr_low, 2),
        "tc": round(cpr_high, 2),
        "r1": round((2 * pivot) - low, 2),
        "r2": round(pivot + (high - low), 2),
        "s1": round((2 * pivot) - high, 2),
        "s2": round(pivot - (high - low), 2),
        "width_pct": round(((cpr_high - cpr_low) / close) * 100, 3),
    }


def get_gift_nifty(env: dict[str, str], nifty_close: float) -> dict:
    manual = env.get("GIFT_NIFTY_LEVEL") or env.get("GIFT_NIFTY_MANUAL")
    if manual:
        try:
            level = float(manual.replace(",", ""))
            return {
                "level": round(level, 2),
                "change_pct": pct_change(level, nifty_close),
                "source": "Manual .env override",
                "note": "Set GIFT_NIFTY_LEVEL in .env to control this pre-open input.",
            }
        except ValueError:
            pass

    symbol = env.get("GIFT_NIFTY_SYMBOL", "").strip()
    if symbol:
        level, change = latest_change(symbol, period="5d")
        if level is not None:
            return {
                "level": level,
                "change_pct": pct_change(level, nifty_close) if nifty_close else change,
                "source": f"Yahoo symbol {symbol}",
                "note": "GIFT Nifty source came from the configured symbol.",
            }

    return {
        "level": None,
        "change_pct": None,
        "source": "Not configured",
        "note": "Add GIFT_NIFTY_LEVEL or GIFT_NIFTY_SYMBOL in .env for a live pre-open gap estimate.",
    }


def load_dashboard_context() -> dict:
    if not DASHBOARD_DATA.exists():
        return {}
    try:
        return json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def market_probabilities(score: float) -> dict[str, int]:
    up = min(70, max(30, round(50 + score * 6)))
    down = min(70, max(30, round(50 - score * 6)))
    sideways = max(10, 100 - up - down)
    total = up + down + sideways
    return {
        "up": round(up / total * 100),
        "down": round(down / total * 100),
        "sideways": round(sideways / total * 100),
    }


def main() -> None:
    env = read_env()
    now = datetime.now(TZ)
    nifty = load_nifty()
    if nifty.empty:
        raise RuntimeError("Could not download Nifty data.")

    nifty["ma20"] = nifty["Close"].rolling(20).mean()
    nifty["ma50"] = nifty["Close"].rolling(50).mean()
    nifty["ma200"] = nifty["Close"].rolling(200).mean()
    latest = nifty.iloc[-1]
    prev = nifty.iloc[-1]
    close = float(latest["Close"])
    close_5d = float(nifty["Close"].iloc[-6]) if len(nifty) > 6 else close
    close_20d = float(nifty["Close"].iloc[-21]) if len(nifty) > 21 else close
    cpr = calculate_cpr(prev)
    recent = nifty.tail(6).copy()
    rsi_value = rsi(nifty["Close"])

    global_rows = []
    global_score = 0.0
    for name, symbol in GLOBAL_SYMBOLS.items():
        level, change = latest_change(symbol)
        if change is not None and name not in {"USD/INR", "Brent Crude", "Gold"}:
            global_score += 1 if change > 0 else -1
        global_rows.append({"name": name, "symbol": symbol, "level": level, "change_pct": change})

    gift = get_gift_nifty(env, close)
    gift_change = gift.get("change_pct")
    trend_score = 0.0
    trend_score += 1 if close > float(latest["ma20"]) else -1
    trend_score += 1 if close > float(latest["ma50"]) else -1
    trend_score += 1 if close > float(latest["ma200"]) else -1
    trend_score += 0.5 if float(latest["ma50"]) > float(latest["ma200"]) else -0.5
    trend_score += 1 if pct_change(close, close_5d) and pct_change(close, close_5d) > 0 else -1
    trend_score += max(-1, min(1, global_score / 4))
    if gift_change is not None:
        trend_score += 1 if gift_change > 0.15 else -1 if gift_change < -0.15 else 0
    probabilities = market_probabilities(trend_score)

    bias = "Positive" if probabilities["up"] > probabilities["down"] else "Negative" if probabilities["down"] > probabilities["up"] else "Neutral"
    opening = "flat to mildly positive"
    if gift_change is not None:
        if gift_change > 0.35:
            opening = "gap-up"
        elif gift_change > 0.10:
            opening = "mildly positive"
        elif gift_change < -0.35:
            opening = "gap-down"
        elif gift_change < -0.10:
            opening = "mildly negative"

    dashboard = load_dashboard_context()
    leading_industries = dashboard.get("leadingIndustries", [])[:8]
    fundamental_picks = dashboard.get("fundamentalPicks", [])[:8]

    payload = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "title": f"Pre-market Technical Blog - {now.strftime('%d %b %Y')}",
        "headline": f"Nifty pre-market bias is {bias.lower()}, with expected opening {opening}.",
        "probabilities": probabilities,
        "nifty": {
            "close": round(close, 2),
            "change_5d_pct": pct_change(close, close_5d),
            "change_20d_pct": pct_change(close, close_20d),
            "ma20": round(float(latest["ma20"]), 2),
            "ma50": round(float(latest["ma50"]), 2),
            "ma200": round(float(latest["ma200"]), 2),
            "rsi14": rsi_value,
        },
        "cpr": cpr,
        "gift_nifty": gift,
        "global_markets": global_rows,
        "pattern_notes": [
            f"Nifty is {'above' if close > float(latest['ma20']) else 'below'} 20 DMA, {'above' if close > float(latest['ma50']) else 'below'} 50 DMA, and {'above' if close > float(latest['ma200']) else 'below'} 200 DMA.",
            f"Last 5-session return is {pct_change(close, close_5d)}%, while last 20-session return is {pct_change(close, close_20d)}%.",
            f"CPR width is {cpr['width_pct']}%; a narrow CPR can support trend expansion, while a wide CPR can invite range-bound action.",
            f"Immediate bullish confirmation improves above TC {cpr['tc']} and R1 {cpr['r1']}; weakness increases below BC {cpr['bc']} and S1 {cpr['s1']}.",
        ],
        "blog_sections": [
            {
                "heading": "Market Opening View",
                "body": f"Based on the available GIFT Nifty input, global-market tone and Nifty trend structure, the market can open {opening}. Treat the first 15-30 minutes as confirmation because large gaps can reverse if price fails near CPR or previous-day extremes.",
            },
            {
                "heading": "Technical View For The Day",
                "body": f"Above CPR top {cpr['tc']}, buyers have better control and the first upside zones are {cpr['r1']} and {cpr['r2']}. Below CPR bottom {cpr['bc']}, sellers gain control and downside zones are {cpr['s1']} and {cpr['s2']}. A sustained move around pivot {cpr['pivot']} can mean a range day.",
            },
            {
                "heading": "What Can Make The Market Go Up",
                "body": "Positive global cues, GIFT Nifty premium, Nifty holding above CPR, and leadership from Stage 2/RRG leading industries can support upside follow-through.",
            },
            {
                "heading": "What Can Make The Market Go Down",
                "body": "Weak Asian markets, rising USD/INR or crude pressure, GIFT Nifty discount, and Nifty failing below CPR/S1 can increase downside risk.",
            },
        ],
        "leading_industries": [
            {"name": row.get("name"), "rs_ratio": row.get("rs_ratio"), "rs_momentum": row.get("rs_momentum")}
            for row in leading_industries
        ],
        "fundamental_picks": [
            {
                "name": row.get("name"),
                "symbol": row.get("display_symbol") or row.get("symbol"),
                "industry": row.get("parent"),
                "score": row.get("fundamental_score"),
            }
            for row in fundamental_picks
        ],
        "recent_nifty": [
            {
                "date": idx.strftime("%Y-%m-%d"),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
            }
            for idx, row in recent.iterrows()
        ],
        "disclaimer": "Educational market note only; not investment advice. Confirm price action after market open and manage risk.",
    }

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    BLOG_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(BLOG_JSON)


if __name__ == "__main__":
    main()
