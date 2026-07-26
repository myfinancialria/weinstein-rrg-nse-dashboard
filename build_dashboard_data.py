from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from screener_fundamentals import add_screener_fundamentals
from screener_segments import fetch_segments_map
from tickertape_fundamentals import add_tickertape_fundamentals


BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
DASHBOARD_DIR = BASE_DIR / "dashboard"
DATA_PATH = DASHBOARD_DIR / "dashboard_data.json"
# Per-symbol lazy-loaded chart files (git-ignored; generated at build time and
# shipped in the GitHub Pages artifact, not committed — see .gitignore).
CHARTS_DIR = DASHBOARD_DIR / "charts"
CHART_WEEKLY_YEARS = 20  # weekly candles history
CHART_DAILY_YEARS = 10   # daily candles history (interval toggle in the drawer)
FUNDAMENTALS_CACHE = REPORTS_DIR / "yahoo_fundamentals_cache.json"
BACKTEST_JSON = DASHBOARD_DIR / "backtest_results.json"
BACKTEST_52W_JSON = DASHBOARD_DIR / "backtest_52w_high.json"

def _latest_report(pattern: str) -> Path | None:
    """Newest reports/<pattern> by date-stamped filename (ISO dates sort
    lexicographically), so the dashboard always picks up the most recent
    industry/Weinstein/RRG scan instead of a hardcoded date."""
    matches = sorted(REPORTS_DIR.glob(pattern))
    return matches[-1] if matches else None


# The industry-scan outputs (Weinstein stage + RRG quadrant + stock rankings)
# are date-stamped; read the newest so a daily re-scan flows through.
INDUSTRY_CSV = _latest_report("screener_industry_weinstein_rrg_*.csv")
STOCK_CSV = _latest_report("screener_industry_stock_rankings_*.csv")
PRODUCT_XLSX = _latest_report("stage2_rrg_leading_industries_best_stocks_products_*.xlsx")


def clean_value(value):
    if isinstance(value, (list, dict)):
        return value
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def clean_records(df: pd.DataFrame) -> list[dict]:
    records = []
    for record in df.to_dict(orient="records"):
        records.append({key: clean_value(value) for key, value in record.items()})
    return records


def add_trade_levels(stocks: pd.DataFrame) -> pd.DataFrame:
    stocks = stocks.copy()
    stocks["cmp"] = pd.to_numeric(stocks["close"], errors="coerce").round(2)
    stocks["entry_price"] = stocks["cmp"]
    weekly_ma = pd.to_numeric(stocks["weekly_ma"], errors="coerce")
    eight_pct_stop = stocks["cmp"] * 0.92
    valid_ma_stop = weekly_ma.where(weekly_ma < stocks["cmp"])
    stocks["sl"] = pd.concat([valid_ma_stop, eight_pct_stop], axis=1).max(axis=1).round(2)
    risk = stocks["entry_price"] - stocks["sl"]
    stocks["target"] = (stocks["entry_price"] + (2 * risk)).round(2)
    invalid = (
        stocks["cmp"].isna()
        | stocks["sl"].isna()
        | (risk <= 0)
    )
    stocks.loc[invalid, ["entry_price", "sl", "target"]] = None
    return stocks


def add_display_symbol(stocks: pd.DataFrame) -> pd.DataFrame:
    stocks = stocks.copy()
    stocks["display_symbol"] = (
        stocks["symbol"]
        .astype(str)
        .str.replace(r"\.NS$", "", regex=True)
        .str.replace(r"\.BO$", "", regex=True)
    )
    return stocks


def pct_return(series: pd.Series, periods: int) -> float | None:
    series = series.dropna()
    if len(series) <= periods:
        return None
    current = series.iloc[-1]
    previous = series.iloc[-periods - 1]
    if previous == 0 or pd.isna(previous) or pd.isna(current):
        return None
    return round(float((current / previous - 1) * 100), 2)


def download_universe_prices(symbols: list[str]) -> pd.DataFrame | None:
    """Single 6-year daily pull for the whole universe, reused for return
    metrics, the 52-week-high breakout scan and the drawer charts so the daily
    job only hits Yahoo once for prices."""
    symbols = sorted(set(symbols))
    if not symbols:
        return None
    try:
        prices = yf.download(
            symbols,
            period="6y",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=True,
            group_by="column",
        )
    except Exception:
        return None
    if prices is None or prices.empty:
        return None
    return prices


def _symbol_series(prices: pd.DataFrame, field: str, symbol: str) -> pd.Series | None:
    try:
        if isinstance(prices.columns, pd.MultiIndex):
            series = prices[(field, symbol)]
        else:
            series = prices[field]
        return series.dropna()
    except Exception:
        return None


def add_return_metrics(stocks: pd.DataFrame, prices: pd.DataFrame | None = None) -> pd.DataFrame:
    stocks = stocks.copy()
    symbols = sorted(stocks["symbol"].dropna().unique().tolist())
    for column in ["return_1w", "return_1m", "return_3m", "return_6m", "return_1y", "return_3y", "return_5y"]:
        stocks[column] = None
    if not symbols:
        return stocks
    if prices is None:
        prices = download_universe_prices(symbols)
    if prices is None or prices.empty:
        return stocks
    close = prices["Close"] if isinstance(prices.columns, pd.MultiIndex) else prices[["Close"]].rename(columns={"Close": symbols[0]})
    windows = {
        "return_1w": 5,
        "return_1m": 21,
        "return_3m": 63,
        "return_6m": 126,
        "return_1y": 252,
        "return_3y": 756,
        "return_5y": 1260,
    }
    for symbol in symbols:
        if symbol not in close.columns:
            continue
        mask = stocks["symbol"] == symbol
        series = close[symbol]
        for column, periods in windows.items():
            stocks.loc[mask, column] = pct_return(series, periods)
    return stocks


def compute_52w_high(prices: pd.DataFrame | None, symbols: list[str]) -> dict[str, dict]:
    """Weekly 52-week-high breakout scan. A stock is flagged when its most
    recent weekly close is at/above the highest of the prior 52 weeks' highs -
    i.e. the latest weekly candle closed above the 52-week high."""
    info: dict[str, dict] = {}
    if prices is None or prices.empty:
        return info
    for symbol in symbols:
        high = _symbol_series(prices, "High", symbol)
        close = _symbol_series(prices, "Close", symbol)
        if high is None or close is None or high.empty or close.empty:
            continue
        weekly_high = high.resample("W-FRI").max().dropna()
        weekly_close = close.resample("W-FRI").last().dropna()
        prior_52w_high = weekly_high.rolling(52).max().shift(1)
        joined = pd.concat([weekly_close.rename("close"), prior_52w_high.rename("high_52w")], axis=1).dropna()
        if joined.empty:
            continue
        last = joined.iloc[-1]
        latest_close = float(last["close"])
        high_52w = float(last["high_52w"])
        above = latest_close >= high_52w and high_52w > 0
        info[symbol] = {
            "high_52w": round(high_52w, 2),
            "weekly_close": round(latest_close, 2),
            "weekly_close_above_52wh": bool(above),
            "pct_above_52wh": round((latest_close / high_52w - 1) * 100, 2) if high_52w > 0 else None,
            "breakout_week": joined.index[-1].date().isoformat(),
        }
    return info


def add_52w_high_flag(stocks: pd.DataFrame, info: dict[str, dict]) -> pd.DataFrame:
    stocks = stocks.copy()
    stocks["high_52w"] = stocks["symbol"].map(lambda s: (info.get(s) or {}).get("high_52w"))
    stocks["weekly_close_above_52wh"] = stocks["symbol"].map(lambda s: bool((info.get(s) or {}).get("weekly_close_above_52wh")))
    stocks["pct_above_52wh"] = stocks["symbol"].map(lambda s: (info.get(s) or {}).get("pct_above_52wh"))
    stocks["breakout_week"] = stocks["symbol"].map(lambda s: (info.get(s) or {}).get("breakout_week"))
    return stocks


def chart_filename(symbol: str) -> str:
    """URL/filesystem-safe basename for a symbol's chart file: drop the ``.NS``
    suffix and replace anything outside ``[A-Za-z0-9_-]`` (e.g. the ``&`` in
    ``M&M.NS``). The frontend derives the same name from ``stock.symbol``, so the
    two must stay in lock-step."""
    base = re.sub(r"\.NS$", "", symbol, flags=re.IGNORECASE)
    return re.sub(r"[^A-Za-z0-9_-]", "_", base)


def _ohlc_bars(prices: pd.DataFrame, symbol: str, weekly: bool, max_bars: int) -> list[dict]:
    """OHLC bars for one symbol, newest ``max_bars`` kept. ``weekly`` resamples to
    W-FRI (Weinstein timeframe); otherwise raw daily bars. Falls back to
    close-only when OHLC fields are missing; the frontend renders candles when
    open/high/low are present and an area chart otherwise."""
    close = _symbol_series(prices, "Close", symbol)
    if close is None or close.empty:
        return []
    open_ = _symbol_series(prices, "Open", symbol)
    high = _symbol_series(prices, "High", symbol)
    low = _symbol_series(prices, "Low", symbol)
    if open_ is None or high is None or low is None:
        series = close.resample("W-FRI").last().dropna() if weekly else close.dropna()
        series = series.tail(max_bars)
        return [
            {"time": idx.strftime("%Y-%m-%d"), "close": round(float(value), 2)}
            for idx, value in series.items()
        ]
    frame = pd.concat({"open": open_, "high": high, "low": low, "close": close}, axis=1).dropna()
    if frame.empty:
        return []
    if weekly:
        frame = frame.resample("W-FRI").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}
        ).dropna()
    frame = frame.tail(max_bars)
    return [
        {
            "time": idx.strftime("%Y-%m-%d"),
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
        }
        for idx, row in frame.iterrows()
    ]


def write_chart_files(
    symbols: list[str],
    out_dir: Path = CHARTS_DIR,
    weekly_years: int = CHART_WEEKLY_YEARS,
    daily_years: int = CHART_DAILY_YEARS,
) -> int:
    """Write one compact JSON per symbol that the drawer lazy-loads on open —
    ``charts/<chart_filename(symbol)>.json`` — with both interval series:
    ``{"weekly": [...~20yr...], "daily": [...~10yr...]}`` (the drawer has a
    weekly/daily toggle).

    Uses a separate full-history (``period='max'``) pull so the shared 6-year
    universe pull that feeds return metrics and the 52-week-high scan stays
    light. These files are git-ignored: generated at build time and shipped in
    the Pages artifact, never committed (they would change daily). Returns the
    number of symbols with data."""
    symbols = sorted({s for s in symbols if s})
    if not symbols:
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        prices = yf.download(
            symbols,
            period="max",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=True,
            group_by="column",
        )
    except Exception:
        prices = None
    if prices is None or prices.empty:
        return 0

    weekly_max = weekly_years * 53   # 53 covers 52 ISO weeks + partial boundary weeks
    daily_max = daily_years * 260    # ~260 trading days/year
    written = 0
    for symbol in symbols:
        payload = {
            "weekly": _ohlc_bars(prices, symbol, weekly=True, max_bars=weekly_max),
            "daily": _ohlc_bars(prices, symbol, weekly=False, max_bars=daily_max),
        }
        path = out_dir / f"{chart_filename(symbol)}.json"
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        if payload["weekly"] or payload["daily"]:
            written += 1
    return written


def add_yahoo_fundamentals(stocks: pd.DataFrame) -> pd.DataFrame:
    stocks = stocks.copy()
    text_fields = [
        "company_description",
        "website",
        "yahoo_sector",
        "yahoo_industry",
    ]
    numeric_fields = [
        "eps",
        "gross_margin_pct",
        "operating_margin_pct",
        "net_margin_pct",
        "roe_pct",
        "debt_to_equity",
        "current_ratio",
        "quick_ratio",
        "free_cash_flow_cr",
        "pe_ratio",
        "pb_ratio",
        "peg_ratio",
    ]
    for column in text_fields:
        stocks[column] = ""
    for column in numeric_fields:
        stocks[column] = np.nan
    stocks["promoter_or_insider_holding_pct"] = np.nan
    stocks["company_description"] = stocks.get("common_product_business", "")
    stocks["yahoo_sector"] = stocks.get("sector", "")
    if os.getenv("SKIP_YAHOO_FUNDAMENTALS") == "1":
        return stocks

    cache = {}
    if FUNDAMENTALS_CACHE.exists():
        try:
            cache = json.loads(FUNDAMENTALS_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}

    def percent(value):
        return round(float(value) * 100, 2) if value is not None else None

    def fetch(symbol: str) -> tuple[str, dict]:
        try:
            info = yf.Ticker(symbol).get_info()
        except Exception:
            info = {}
        result = {
            "company_description": info.get("longBusinessSummary", "") or info.get("businessSummary", ""),
            "website": info.get("website", ""),
            "yahoo_sector": info.get("sector", ""),
            "yahoo_industry": info.get("industry", ""),
            "promoter_or_insider_holding_pct": percent(info.get("heldPercentInsiders")),
            "eps": info.get("trailingEps"),
            "gross_margin_pct": percent(info.get("grossMargins")),
            "operating_margin_pct": percent(info.get("operatingMargins")),
            "net_margin_pct": percent(info.get("profitMargins")),
            "roe_pct": percent(info.get("returnOnEquity")),
            "debt_to_equity": info.get("debtToEquity"),
            "current_ratio": info.get("currentRatio"),
            "quick_ratio": info.get("quickRatio"),
            "free_cash_flow_cr": round(float(info.get("freeCashflow")) / 10_000_000, 2) if info.get("freeCashflow") is not None else None,
            "pe_ratio": info.get("trailingPE"),
            "pb_ratio": info.get("priceToBook"),
            "peg_ratio": info.get("pegRatio"),
        }
        return symbol, result

    symbols = stocks["symbol"].dropna().unique().tolist()
    missing = [symbol for symbol in symbols if symbol not in cache]
    if missing:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(fetch, symbol) for symbol in missing]
            for future in as_completed(futures):
                symbol, result = future.result()
                cache[symbol] = result
        FUNDAMENTALS_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    for symbol in symbols:
        info = cache.get(symbol, {})
        mask = stocks["symbol"] == symbol
        for field, value in info.items():
            if field in stocks.columns and value not in {"", None}:
                stocks.loc[mask, field] = value
    return stocks


def make_pros(row: pd.Series) -> list[str]:
    pros = []
    if row.get("stage") == "stage_2":
        pros.append("Price structure qualifies as Weinstein Stage 2.")
    if row.get("rrg_quadrant") == "leading":
        pros.append("RRG is in Leading quadrant versus Nifty 50.")
    if (row.get("rs_momentum") or 0) >= 101:
        pros.append("Relative momentum is strong.")
    if (row.get("weekly_ma_slope_4w_pct") or 0) > 0:
        pros.append("30-week moving average is rising.")
    if (row.get("roce") or 0) >= 15:
        pros.append("ROCE is healthy versus many listed peers.")
    if (row.get("roe_pct") or 0) >= 15:
        pros.append("ROE indicates efficient use of shareholder capital.")
    if (row.get("net_margin_pct") or 0) >= 10:
        pros.append("Net margin is in double digits.")
    if (row.get("free_cash_flow_cr") or 0) > 0:
        pros.append("Free cash flow is positive.")
    if (row.get("return_6m") or 0) > 0 and (row.get("return_1y") or 0) > 0:
        pros.append("Medium-term returns are positive.")
    return pros[:5]


def make_red_flags(row: pd.Series) -> list[str]:
    flags = []
    if row.get("stage") != "stage_2":
        flags.append("Stock itself is not Stage 2 even though the industry qualifies.")
    if row.get("rrg_quadrant") not in {"leading", "improving"}:
        flags.append("RRG is not in a positive quadrant.")
    if pd.notna(row.get("pe")) and row.get("pe") and row.get("pe") > 80:
        flags.append("High P/E; valuation risk is elevated.")
    if pd.notna(row.get("roce")) and row.get("roce") < 10:
        flags.append("ROCE is below 10%.")
    if pd.notna(row.get("debt_to_equity")) and row.get("debt_to_equity") > 100:
        flags.append("Debt-to-equity is elevated.")
    if pd.notna(row.get("current_ratio")) and row.get("current_ratio") < 1:
        flags.append("Current ratio is below 1.0.")
    if pd.notna(row.get("quick_ratio")) and row.get("quick_ratio") < 1:
        flags.append("Quick ratio is below 1.0.")
    if pd.notna(row.get("free_cash_flow_cr")) and row.get("free_cash_flow_cr") < 0:
        flags.append("Free cash flow is negative.")
    if (row.get("return_1m") or 0) < -5:
        flags.append("1-month return is weak.")
    if pd.isna(row.get("cmp")):
        flags.append("Latest price data was unavailable from Yahoo.")
    return flags[:5]


def _swot_num(value, suffix: str = "") -> str:
    """Format a numeric field for SWOT prose; '' if the value is missing."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if abs(num) >= 100:
        return f"{num:,.0f}{suffix}"
    return f"{num:.1f}{suffix}"


def make_swot(row: pd.Series) -> dict[str, list[str]]:
    """Company-only SWOT built from the business itself.

    Strengths and Weaknesses are INTERNAL company factors (profitability,
    balance sheet, cash flow, earnings momentum, ownership). Opportunities and
    Threats are EXTERNAL factors (valuation environment, end-market demand,
    interest rates, competition and regulation). Price-action / RRG /
    moving-average signals are intentionally excluded here — those live in the
    technical (Pros / Red Flags / Returns) sections of the drawer.
    """
    strengths: list = []
    weaknesses: list = []
    opportunities: list = []
    threats: list = []

    def val(key):
        v = row.get(key)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return v

    # --- Internal: profitability & capital efficiency ---
    roe = val("roe_pct")
    if roe is not None:
        if roe >= 15:
            strengths.append(f"Strong return on equity ({_swot_num(roe, '%')}) — capital is used efficiently.")
        else:
            weaknesses.append(f"Modest return on equity ({_swot_num(roe, '%')}) — capital efficiency has room to improve.")

    roce = val("roce")
    if roce is not None and roce >= 15:
        strengths.append(f"Healthy return on capital employed ({_swot_num(roce, '%')}) points to a profitable core business.")
    elif roce is not None and roce < 8:
        weaknesses.append(f"Low return on capital employed ({_swot_num(roce, '%')}) — the core business earns thin returns.")

    nm = val("net_margin_pct")
    if nm is not None:
        if nm >= 10:
            strengths.append(f"Comfortable net margin ({_swot_num(nm, '%')}) reflects pricing power and cost control.")
        else:
            weaknesses.append(f"Thin net margin ({_swot_num(nm, '%')}) leaves little cushion if costs rise.")

    # --- Internal: cash flow & balance sheet ---
    fcf = val("free_cash_flow_cr")
    if fcf is not None:
        if fcf > 0:
            strengths.append(f"Positive free cash flow ({_swot_num(fcf)} Cr) funds growth, dividends or debt reduction internally.")
        else:
            weaknesses.append(f"Negative free cash flow ({_swot_num(fcf)} Cr) — the business is not yet self-funding.")

    de = val("debt_to_equity")
    if de is not None:
        if de <= 50:
            strengths.append(f"Low leverage (debt/equity {_swot_num(de)}) gives a resilient balance sheet.")
        elif de > 100:
            weaknesses.append(f"High leverage (debt/equity {_swot_num(de)}) makes profits sensitive to funding costs.")

    cr = val("current_ratio")
    if cr is not None:
        if cr >= 1:
            strengths.append(f"Adequate short-term liquidity (current ratio {_swot_num(cr)}).")
        else:
            weaknesses.append(f"Tight short-term liquidity (current ratio {_swot_num(cr)}).")

    # --- Internal: earnings momentum & ownership ---
    qpg = val("qtr_profit_var_pct")
    if qpg is not None:
        if qpg > 0:
            strengths.append(f"Quarterly profit is growing year-on-year ({_swot_num(qpg, '%')}), showing earnings momentum.")
        else:
            weaknesses.append(f"Quarterly profit fell year-on-year ({_swot_num(qpg, '%')}), a sign of near-term earnings pressure.")

    hold = val("promoter_or_insider_holding_pct")
    if hold is not None and hold >= 50:
        strengths.append(f"High promoter/insider holding ({_swot_num(hold, '%')}) signals strong management commitment.")
    elif hold is not None and hold < 30:
        weaknesses.append(f"Low promoter/insider holding ({_swot_num(hold, '%')}) means less management skin in the game.")

    dy = val("dividend_yield_pct")
    if dy is not None and dy >= 1.5:
        strengths.append(f"Regular dividend ({_swot_num(dy, '%')} yield) returns cash to shareholders.")

    # --- External: valuation environment ---
    pe = val("pe_ratio")
    if pe is None:
        pe = val("pe")
    peg = val("peg_ratio")
    if (pe is not None and pe < 25) or (peg is not None and 0 < peg < 1):
        bits = []
        if pe is not None:
            bits.append(f"P/E {_swot_num(pe)}")
        if peg is not None and peg > 0:
            bits.append(f"PEG {_swot_num(peg)}")
        opportunities.append(f"Undemanding valuation ({', '.join(bits)}) leaves room for re-rating if execution holds.")
    if (pe is not None and pe > 60) or (peg is not None and peg > 2.5):
        threats.append(f"Rich valuation (P/E {_swot_num(pe)}) raises de-rating risk if growth disappoints.")

    # --- External: end-market demand & macro ---
    qsg = val("qtr_sales_var_pct")
    if qsg is not None and qsg >= 15:
        opportunities.append(f"Strong revenue growth ({_swot_num(qsg, '%')} YoY) suggests expanding end-market demand to capture.")
    elif qsg is not None and qsg < 0:
        threats.append(f"Revenue contracted year-on-year ({_swot_num(qsg, '%')}), pointing to softer demand.")

    if de is not None and de > 100:
        threats.append("Elevated debt exposes earnings to rising interest rates.")

    # Fallbacks so no quadrant is ever empty
    if not strengths:
        strengths.append("No standout financial strength in the current fundamentals.")
    if not weaknesses:
        weaknesses.append("No major financial weakness flagged in the current fundamentals.")
    if not opportunities:
        opportunities.append("Sector tailwinds, new products or operating leverage could open upside (see industry outlook below).")
    if not threats:
        threats.append("Competition, input-cost inflation, a macro slowdown or regulatory change are the key external risks.")

    return {
        "strengths": strengths[:4],
        "weaknesses": weaknesses[:4],
        "opportunities": opportunities[:4],
        "threats": threats[:4],
    }


INDUSTRY_THESES = {
    "Microfinance Institutions": {
        "relevance_now": "Rural credit demand, financial inclusion, and small-ticket business lending keep the sector in focus when credit growth broadens beyond large urban borrowers.",
        "future_possibilities": "Digitized collections, co-lending, better risk scoring, cross-selling insurance/savings products, and formalization of rural borrowers can support growth if asset quality stays controlled.",
        "policy_support": "Supported indirectly by India's financial inclusion agenda, Jan Dhan/account penetration, digital payments infrastructure, SHG-bank linkage, and RBI's dedicated microfinance regulatory framework. Watch for tighter rules if borrower stress rises.",
        "policy_sources": "RBI microfinance framework; government financial inclusion programs.",
    },
    "Telecom - Infrastructure": {
        "relevance_now": "Data consumption, 5G densification, fibre rollout, and tower tenancy demand make telecom infrastructure strategically important.",
        "future_possibilities": "More fibre-to-tower, 5G small cells, rural broadband, enterprise networks, and private 5G can expand addressable demand.",
        "policy_support": "Government programs such as Digital India, BharatNet, and telecom PLI/Make-in-India priorities are supportive for domestic telecom infrastructure and equipment.",
        "policy_sources": "Digital India, BharatNet, telecom PLI references.",
    },
    "Meat Products including Poultry": {
        "relevance_now": "Protein consumption is rising with income growth, urbanization, and organized food supply chains.",
        "future_possibilities": "Branded poultry, processed meat, cold-chain expansion, exports, and value-added egg/meat products can increase margins.",
        "policy_support": "Food processing, cold-chain, animal husbandry, and export-support policies are broadly supportive, though disease outbreaks and export restrictions remain key risks.",
        "policy_sources": "Food processing and animal husbandry policy ecosystem.",
    },
    "Auto Components & Equipments": {
        "relevance_now": "Auto demand, EV localization, premiumization, and export opportunities are pulling domestic component suppliers into focus.",
        "future_possibilities": "EV drivetrains, electronics, lightweight components, sensors, ADAS, and global sourcing from India can create multi-year opportunities.",
        "policy_support": "Government support is positive through PLI for automobiles/auto components, FAME/EV ecosystem support, and Make in India localization priorities.",
        "policy_sources": "Auto and auto-components PLI; EV policy ecosystem.",
    },
    "Abrasives & Bearings": {
        "relevance_now": "Industrial capex, manufacturing, railways, autos, and infrastructure drive demand for bearings, abrasives, and precision consumables.",
        "future_possibilities": "Higher domestic manufacturing, replacement demand, industrial automation, and export-led precision components can support leaders.",
        "policy_support": "Indirectly supported by Make in India, infrastructure capex, railway modernization, and manufacturing PLI-led supply-chain localization.",
        "policy_sources": "Make in India; infrastructure and manufacturing policy.",
    },
    "Other Industrial Products": {
        "relevance_now": "Industrial capex and infrastructure execution are improving demand for specialized engineering products.",
        "future_possibilities": "Import substitution, defence/railway/electronics supply chains, and niche exports can create winners with execution strength.",
        "policy_support": "Supported indirectly by infrastructure spending, Make in India, railway modernization, defence indigenisation, and PLI-driven manufacturing.",
        "policy_sources": "Manufacturing, railways, defence and infrastructure policy ecosystem.",
    },
    "Other Bank": {
        "relevance_now": "Small finance banks benefit when deposit franchises mature and credit demand remains broad-based.",
        "future_possibilities": "Secured retail loans, MSME lending, cross-sell, digital banking, and branch productivity can improve profitability.",
        "policy_support": "RBI's small finance bank framework and financial inclusion priorities are supportive, while regulation remains strict around capital and asset quality.",
        "policy_sources": "RBI small finance bank framework; financial inclusion priorities.",
    },
    "Cables - Electricals": {
        "relevance_now": "Power transmission, housing, data centers, renewables, railways, and industrial capex are driving wire and cable demand.",
        "future_possibilities": "Premium branded wires, EHV cables, railway/solar/data-center demand, and export opportunities can sustain growth.",
        "policy_support": "Supported indirectly by infrastructure capex, housing, renewable energy targets, power grid expansion, and domestic manufacturing priorities.",
        "policy_sources": "Power infrastructure, housing, renewable energy and Make in India policy ecosystem.",
    },
    "Aerospace & Defense": {
        "relevance_now": "Defence indigenisation, order books, exports, and electronics-heavy platforms make this one of the most policy-backed themes.",
        "future_possibilities": "Radars, avionics, simulators, drones, missiles, space components, and export orders can expand the opportunity set.",
        "policy_support": "Government support is strong through Make in India, defence indigenisation, import embargo/positive lists, procurement preference, and rising defence production/export goals.",
        "policy_sources": "Defence indigenisation and defence production/export policy references.",
    },
    "Asset Management Company": {
        "relevance_now": "Financialization of household savings, SIP growth, and equity participation keep AMCs structurally relevant.",
        "future_possibilities": "Passive funds, retirement products, alternatives, ETFs, deeper B30 penetration, and digital distribution can support AUM growth.",
        "policy_support": "Supported indirectly by capital-market formalization, retirement/investment product penetration, and SEBI's regulated mutual fund framework.",
        "policy_sources": "SEBI mutual fund framework; financialization trend.",
    },
    "Aluminium, Copper & Zinc Products": {
        "relevance_now": "Electrification, power equipment, EVs, construction, and industrial capex are increasing demand for conductive metals products.",
        "future_possibilities": "Copper winding wires, aluminium recycling, transformers, motors, renewables, and EV supply chains offer growth pathways.",
        "policy_support": "Supported indirectly by power infrastructure, renewables, EV localization, recycling priorities, and manufacturing localization policies.",
        "policy_sources": "Power, renewable energy, EV and manufacturing policy ecosystem.",
    },
    "2/3 Wheelers": {
        "relevance_now": "Two- and three-wheelers are the backbone of Indian personal and last-mile mobility; rural recovery, replacement demand and the EV transition keep the segment in focus.",
        "future_possibilities": "Electric 2W/3W adoption, financing penetration, premiumisation, exports to Africa/ASEAN and connected-vehicle features can extend the growth runway.",
        "policy_support": "Supported by FAME-II/EV subsidies, state EV policies, PLI for auto and advanced chemistry cells, and Make in India localisation; watch for subsidy tapering.",
        "policy_sources": "FAME-II; state EV policies; auto/ACC PLI.",
    },
    "Passenger Cars & Utility Vehicles": {
        "relevance_now": "SUV-led premiumisation, rising incomes and low car penetration versus global peers keep passenger vehicles a structural consumption theme.",
        "future_possibilities": "EV and hybrid line-ups, connected/ADAS features, CNG variants, exports and financing depth can expand the addressable market.",
        "policy_support": "Backed by auto PLI, FAME/EV incentives, scrappage policy and Make in India; emission (BS-VI/CAFE) norms shape the product mix.",
        "policy_sources": "Auto PLI; FAME-II; vehicle scrappage policy.",
    },
    "Advertising & Media Agencies": {
        "relevance_now": "Ad spends track nominal GDP and the shift to digital; agencies benefit as brands formalise marketing and move budgets online.",
        "future_possibilities": "Programmatic/digital advertising, retail media, data-led targeting, regional-language content and performance marketing can lift margins.",
        "policy_support": "No direct subsidy; indirectly aided by digital-economy growth, Digital India and rising internet/smartphone penetration.",
        "policy_sources": "Digital India; broader digital-economy trend.",
    },
    "Media & Entertainment": {
        "relevance_now": "Rising discretionary spends, streaming, live events and regional content keep media relevant even as consumption shifts from linear TV to digital.",
        "future_possibilities": "OTT monetisation, gaming, live experiences, IP libraries, regional-language reach and ad-plus-subscription models can drive growth.",
        "policy_support": "Supported by AVGC (animation, VFX, gaming, comics) policy focus, ease of filming and a growing creative-economy agenda.",
        "policy_sources": "AVGC policy; creative-economy initiatives.",
    },
    "Film Production, Distribution & Exhibition": {
        "relevance_now": "Box-office recovery, premium formats and a strong regional-cinema slate support content producers and multiplex chains.",
        "future_possibilities": "Premium screens, food & beverage/ad income, streaming syndication, IP creation and regional expansion can improve economics.",
        "policy_support": "Aided by AVGC promotion, single-window filming clearances and GST treatment of tickets; content and screen supply are key swing factors.",
        "policy_sources": "AVGC policy; state film-friendly initiatives.",
    },
    "E-Learning": {
        "relevance_now": "Digital adoption, skilling needs, test-prep demand and enterprise upskilling keep online education structurally relevant.",
        "future_possibilities": "Vernacular content, AI tutoring, certification, B2B/enterprise learning and hybrid models can widen the market beyond metros.",
        "policy_support": "Supported by NEP 2020's digital-education push, Digital India, SWAYAM/DIKSHA platforms and skilling missions.",
        "policy_sources": "NEP 2020; Digital India; national skilling missions.",
    },
    "Consulting Services": {
        "relevance_now": "Enterprise digital transformation, ESG, GCC (global capability centre) expansion and India's services exports keep consulting demand firm.",
        "future_possibilities": "AI/analytics advisory, cloud and ERP transformation, ESG/sustainability consulting and mid-market outsourcing can extend growth.",
        "policy_support": "Indirectly supported by services-export incentives, GCC-friendly state policies and ease-of-doing-business reforms.",
        "policy_sources": "Services-export framework; GCC/state IT-ITeS policies.",
    },
    "Business Process Outsourcing (BPO)/ Knowledge Process Outsourcing (KPO)": {
        "relevance_now": "India's cost and talent advantage, GCC build-out and demand for back-office/analytics keep the BPO/KPO theme durable.",
        "future_possibilities": "Higher-value KPO, automation-plus-human models, analytics, GenAI-augmented delivery and non-voice/domain expertise can raise realisations.",
        "policy_support": "Supported by IT-ITeS/GCC state policies, SEZ/STPI benefits and services-export incentives; automation is both a tailwind and a risk.",
        "policy_sources": "STPI/SEZ framework; state IT-ITeS policies.",
    },
    "Non Banking Financial Company (NBFC)": {
        "relevance_now": "NBFCs fill credit gaps banks under-serve — retail, MSME, vehicle and housing finance — and benefit as credit demand broadens.",
        "future_possibilities": "Co-lending with banks, digital underwriting, secured retail, MSME and affordable-housing loans can support high-quality growth.",
        "policy_support": "RBI's scale-based regulation, co-lending framework and financial-inclusion push are supportive; tighter norms follow any asset-quality stress.",
        "policy_sources": "RBI scale-based regulation; co-lending framework.",
    },
    "Life Insurance": {
        "relevance_now": "Under-penetrated protection, financialisation of savings and a young population make life insurance a long-duration compounding theme.",
        "future_possibilities": "Protection and annuity mix, bancassurance, digital distribution, deeper tier-2/3 reach and product innovation can lift VNB margins.",
        "policy_support": "Supported by IRDAI reforms, 'Insurance for All by 2047', higher FDI limits and tax treatment of long-term savings.",
        "policy_sources": "IRDAI reforms; Insurance for All 2047; FDI norms.",
    },
    "General Insurance": {
        "relevance_now": "Low non-life penetration, rising health and motor demand, and formalisation of assets keep general insurance structurally attractive.",
        "future_possibilities": "Health insurance growth, digital/embedded distribution, better underwriting and government scheme participation can expand premiums.",
        "policy_support": "Aided by IRDAI reforms, Ayushman Bharat/health-cover push, crop and motor mandates and higher FDI limits.",
        "policy_sources": "IRDAI reforms; Ayushman Bharat; motor/crop mandates.",
    },
    "Logistics Solution Provider": {
        "relevance_now": "Manufacturing shift, e-commerce, GST-led warehousing consolidation and multimodal freight demand keep logistics a structural theme.",
        "future_possibilities": "3PL/express, cold-chain, warehousing REIT-isation, port-led logistics, digital freight and multimodal corridors can drive growth.",
        "policy_support": "Strongly supported by PM Gati Shakti, the National Logistics Policy, dedicated freight corridors and multimodal infrastructure push.",
        "policy_sources": "National Logistics Policy; PM Gati Shakti; DFC.",
    },
    "Household Appliances": {
        "relevance_now": "Rising incomes, low appliance penetration, electrification and premiumisation drive durable consumer-appliance demand.",
        "future_possibilities": "Premium and energy-efficient products, air-conditioning penetration, rural reach, exports and backward integration can expand margins.",
        "policy_support": "Supported by PLI for white goods (AC and LED components), Make in India and import-substitution priorities.",
        "policy_sources": "White-goods PLI; Make in India.",
    },
    "Footwear": {
        "relevance_now": "Formalisation, branded-play growth, quality-control norms and rising discretionary spends favour organised footwear makers.",
        "future_possibilities": "Premiumisation, athleisure, domestic manufacturing shift from imports, exports and omnichannel retail can lift share and margins.",
        "policy_support": "Supported by mandatory BIS quality-control orders, footwear/leather export incentives and Make in India localisation.",
        "policy_sources": "BIS quality-control orders; leather/footwear export schemes.",
    },
    "Breweries & Distilleries": {
        "relevance_now": "Premiumisation, a young legal-drinking-age population and rising discretionary spends support beer and spirits volumes.",
        "future_possibilities": "Premium and craft segments, ethanol blending economics for distilleries, exports and portfolio premiumisation can drive value.",
        "policy_support": "Ethanol-blending programme (EBP/E20) is a strong tailwind for distilleries; however, state excise, pricing controls and licensing are key risks.",
        "policy_sources": "Ethanol Blending Programme (E20); state excise policies.",
    },
    "Cigarettes & Tobacco Products": {
        "relevance_now": "Cigarettes offer resilient, cash-generative demand with pricing power, even as volumes face regulatory and taxation pressure.",
        "future_possibilities": "Premium mixes, market-share gains from illicit trade and portfolio diversification into FMCG can sustain earnings.",
        "policy_support": "Highly regulated: heavy GST/cess, advertising bans and public-health measures are structural headwinds; taxation stability is the key swing factor.",
        "policy_sources": "GST/cess on tobacco; COTPA public-health regulation.",
    },
    "Lubricants": {
        "relevance_now": "Lubricant demand tracks vehicle parc, industrial activity and freight movement, offering steady, brand-led cash flows.",
        "future_possibilities": "Premium synthetics, industrial and EV-fluid segments, rural distribution and exports can offset EV-driven auto-lubricant risk.",
        "policy_support": "No direct subsidy; linked to industrial/auto activity, Make in India manufacturing and infrastructure capex.",
        "policy_sources": "Industrial and auto-sector demand ecosystem.",
    },
    "Oil Exploration & Production": {
        "relevance_now": "Domestic E&P underpins energy security; crude/gas price cycles and output from new fields drive earnings.",
        "future_possibilities": "New discoveries, enhanced recovery, gas monetisation and the energy-transition pivot into gas and low-carbon can shape the long term.",
        "policy_support": "Supported by HELP/OALP licensing, gas-pricing reforms and energy-security priorities; windfall taxes and subsidy sharing are risks.",
        "policy_sources": "HELP/OALP; gas-pricing reforms.",
    },
    "Oil Equipment & Services": {
        "relevance_now": "Rising domestic E&P capex, gas infrastructure build-out and refinery expansion support oilfield equipment and services demand.",
        "future_possibilities": "City-gas and pipeline projects, offshore development, refinery upgrades and energy-transition infrastructure can extend order books.",
        "policy_support": "Aided by energy-security push, gas-infrastructure expansion, HELP/OALP activity and Make in India domestic-sourcing preferences.",
        "policy_sources": "HELP/OALP; national gas-grid programme.",
    },
    "Offshore Support Solution Drilling": {
        "relevance_now": "Offshore drilling and support demand rise with higher upstream capex and firm energy prices, a cyclical but high-operating-leverage theme.",
        "future_possibilities": "Higher day-rates, fleet utilisation, deepwater development and gas-focused offshore projects can drive an up-cycle.",
        "policy_support": "Linked to national energy-security and domestic-production goals, HELP/OALP activity and offshore field development.",
        "policy_sources": "Energy-security priorities; HELP/OALP.",
    },
    "LPG/CNG/PNG/LNG Supplier": {
        "relevance_now": "India's gas-economy push, cleaner-fuel adoption and city-gas expansion make gas distributors a structural energy-transition play.",
        "future_possibilities": "City-gas network build-out, CNG vehicle growth, PNG household connections, LNG imports and industrial fuel-switching can expand volumes.",
        "policy_support": "Strongly supported by the push to raise gas in the energy mix to ~15%, city-gas distribution licensing, and cleaner-fuel priorities.",
        "policy_sources": "National gas-grid; city-gas distribution (PNGRB) rounds.",
    },
    "Aluminium": {
        "relevance_now": "Aluminium is a key light-weighting and electrification metal; power, transport, packaging and renewables demand keep it strategically important.",
        "future_possibilities": "EV and solar demand, recycling, downstream value-added products and cost-curve advantages can support integrated producers.",
        "policy_support": "Aided by infrastructure/renewables capex, import-duty structures and Make in India; power costs and global prices are key swing factors.",
        "policy_sources": "Renewable-energy and infrastructure policy; metal-import duties.",
    },
    "Sponge Iron": {
        "relevance_now": "Sponge iron feeds India's growing secondary steel capacity; construction and infrastructure demand drive volumes.",
        "future_possibilities": "Capacity additions, integration into steel/power, pellet and DRI efficiency, and green-steel pathways can support growth.",
        "policy_support": "Supported by infrastructure capex, housing and the National Steel Policy; raw-material (iron ore/coal) access and emissions are key risks.",
        "policy_sources": "National Steel Policy; infrastructure capex.",
    },
    "Other Construction Materials": {
        "relevance_now": "Housing, infrastructure execution and real-estate upcycle drive demand for building and construction materials.",
        "future_possibilities": "Premiumisation, branded building products, tiles/sanitaryware/boards, exports and rural housing can extend the runway.",
        "policy_support": "Supported by PM Awas Yojana, infrastructure and capex push, and housing-for-all priorities.",
        "policy_sources": "PM Awas Yojana; infrastructure capex programmes.",
    },
    "Plastic Products - Industrial": {
        "relevance_now": "Industrial plastics serve piping, infrastructure, agriculture and packaging; water, sanitation and construction demand keep volumes firm.",
        "future_possibilities": "Piping/PVC for water and housing, agri-infrastructure, exports, and value-added engineered plastics can drive growth.",
        "policy_support": "Aided by Jal Jeevan Mission (water/piping), housing, agriculture and infrastructure spending; input (crude/PVC) prices are a key swing factor.",
        "policy_sources": "Jal Jeevan Mission; housing and infrastructure programmes.",
    },
    "Rubber": {
        "relevance_now": "Rubber products serve autos, industry and infrastructure; tyre and industrial demand track vehicle parc and capex cycles.",
        "future_possibilities": "Value-added and specialty rubber, tyre premiumisation, exports and import-substitution can improve realisations.",
        "policy_support": "Supported by auto/tyre PLI-adjacent demand, quality-control orders and Make in India; natural-rubber and crude-derivative prices are risks.",
        "policy_sources": "Auto-sector demand; BIS quality-control orders.",
    },
    "Animal Feed": {
        "relevance_now": "Rising protein consumption, organised poultry/dairy and aquaculture growth drive structural animal-feed demand.",
        "future_possibilities": "Feed prem: additives, aqua and cattle feed, integration with protein producers, and branded/scientific nutrition can lift margins.",
        "policy_support": "Aided by animal-husbandry, dairy and fisheries missions, and food-processing infrastructure support; grain prices are a key input risk.",
        "policy_sources": "Animal husbandry and fisheries development programmes.",
    },
    "Other Agricultural Products": {
        "relevance_now": "Agri-inputs and products benefit from rural demand, food security priorities and the shift to organised, value-added agriculture.",
        "future_possibilities": "Branded agri-products, exports, agri-inputs, processing and farm-mechanisation can expand the addressable market.",
        "policy_support": "Supported by agri-infrastructure funds, MSP/food-security programmes, export incentives and food-processing schemes.",
        "policy_sources": "Agri-Infrastructure Fund; food-processing (PMKSY) schemes.",
    },
    "Medical Equipment & Supplies": {
        "relevance_now": "Rising healthcare spend, hospital expansion, insurance penetration and import-substitution make medical devices a structural theme.",
        "future_possibilities": "Domestic device manufacturing, diagnostics, consumables, exports and premium equipment can reduce import dependence and lift growth.",
        "policy_support": "Supported by PLI for medical devices, med-tech parks, Ayushman Bharat and import-substitution priorities.",
        "policy_sources": "Medical-devices PLI; med-tech parks; Ayushman Bharat.",
    },
    "Real Estate Investment Trusts (REITs)": {
        "relevance_now": "REITs offer regular, rent-yielding exposure to Grade-A commercial real estate as office demand and GCC leasing recover.",
        "future_possibilities": "Retail and warehousing REITs, portfolio expansion, occupancy and rental escalation, and rate-cycle tailwinds can improve total returns.",
        "policy_support": "Enabled by SEBI's REIT framework, favourable taxation of distributions and commercial-real-estate formalisation.",
        "policy_sources": "SEBI REIT regulations; commercial-real-estate formalisation.",
    },
    "Amusement Parks/ Other Recreation": {
        "relevance_now": "Rising discretionary spends, experience-led consumption and urban leisure demand support amusement and recreation operators.",
        "future_possibilities": "New park capacity, premium experiences, food & beverage/retail monetisation, tourism tie-ins and asset-light expansion can drive growth.",
        "policy_support": "Indirectly aided by tourism promotion, ease of doing business and consumption/urban-leisure trends; discretionary and seasonal risks remain.",
        "policy_sources": "Tourism-promotion initiatives; consumption trend.",
    },
    "Other Consumer Services": {
        "relevance_now": "Formalisation of services, rising incomes and organised-play share gains keep consumer-services businesses structurally relevant.",
        "future_possibilities": "Premiumisation, digital delivery, franchise/scale expansion and new-category adoption can extend growth.",
        "policy_support": "Indirectly supported by consumption growth, digital adoption, formalisation and ease-of-doing-business reforms.",
        "policy_sources": "Consumption and formalisation trend; Digital India.",
    },
    "Stationary": {
        "relevance_now": "Stationery and school/office products benefit from education demand, back-to-office normalisation and branded-play formalisation.",
        "future_possibilities": "Premium and branded products, art/craft and office segments, exports and distribution expansion can lift margins.",
        "policy_support": "Indirectly aided by education spending, NEP 2020, GST formalisation and Make in India; input (paper/plastic) prices are a swing factor.",
        "policy_sources": "Education spending; NEP 2020; GST formalisation.",
    },
}


def add_stock_notes(stocks: pd.DataFrame) -> pd.DataFrame:
    stocks = stocks.copy()
    stocks["pros"] = stocks.apply(lambda row: make_pros(row), axis=1)
    stocks["red_flags"] = stocks.apply(lambda row: make_red_flags(row), axis=1)
    stocks["swot"] = stocks.apply(lambda row: make_swot(row), axis=1)
    return stocks


def add_fundamental_scores(stocks: pd.DataFrame) -> pd.DataFrame:
    stocks = stocks.copy()
    for column in [
        "dividend_yield_pct",
        "np_qtr_cr",
        "qtr_profit_var_pct",
        "sales_qtr_cr",
        "qtr_sales_var_pct",
    ]:
        if column not in stocks.columns:
            stocks[column] = np.nan

    def score_row(row: pd.Series) -> tuple[float, list[str]]:
        score = 0.0
        reasons = []

        roce = row.get("roce")
        if pd.notna(roce):
            score += min(max(float(roce), 0), 30) / 30 * 22
            if roce >= 20:
                reasons.append("High ROCE")
            elif roce < 10:
                reasons.append("Low ROCE")

        roe = row.get("roe_pct")
        if pd.notna(roe):
            score += min(max(float(roe), 0), 25) / 25 * 14
            if roe >= 15:
                reasons.append("Good ROE")

        net_margin = row.get("net_margin_pct")
        if pd.notna(net_margin):
            score += min(max(float(net_margin), 0), 20) / 20 * 12
            if net_margin >= 10:
                reasons.append("Healthy net margin")

        pe = row.get("pe_ratio") if pd.notna(row.get("pe_ratio")) else row.get("pe")
        if pd.notna(pe) and pe > 0:
            if pe <= 20:
                score += 14
                reasons.append("Reasonable P/E")
            elif pe <= 40:
                score += 10
            elif pe <= 70:
                score += 5
            else:
                reasons.append("Expensive P/E")

        debt_to_equity = row.get("debt_to_equity")
        if pd.notna(debt_to_equity):
            if debt_to_equity <= 50:
                score += 10
                reasons.append("Debt manageable")
            elif debt_to_equity > 150:
                reasons.append("High leverage")

        current_ratio = row.get("current_ratio")
        if pd.notna(current_ratio) and current_ratio >= 1:
            score += 5
            reasons.append("Liquidity okay")

        free_cash_flow = row.get("free_cash_flow_cr")
        if pd.notna(free_cash_flow):
            if free_cash_flow > 0:
                score += 8
                reasons.append("Positive FCF")
            else:
                reasons.append("Negative FCF")

        qtr_sales = row.get("qtr_sales_var_pct")
        if pd.notna(qtr_sales):
            score += min(max(float(qtr_sales), 0), 50) / 50 * 7
            if qtr_sales > 15:
                reasons.append("Sales growth strong")

        qtr_profit = row.get("qtr_profit_var_pct")
        if pd.notna(qtr_profit):
            score += min(max(float(qtr_profit), 0), 50) / 50 * 8
            if qtr_profit > 15:
                reasons.append("Profit growth strong")

        market_cap = row.get("market_cap_cr")
        if pd.notna(market_cap) and market_cap > 1000:
            score += 5
            reasons.append("Meaningful size/liquidity")

        return round(min(score, 100), 2), reasons[:5]

    scored = stocks.apply(lambda row: score_row(row), axis=1)
    stocks["fundamental_score"] = [item[0] for item in scored]
    stocks["fundamental_reasons"] = [item[1] for item in scored]
    return stocks


def main() -> None:
    industries = pd.read_csv(INDUSTRY_CSV)
    stocks = pd.read_csv(STOCK_CSV)

    product_map = {}
    if PRODUCT_XLSX.exists():
        product_df = pd.read_excel(PRODUCT_XLSX, sheet_name="Stage2 RRG Leading", header=5)
        if "Symbol" in product_df.columns and "Commonly Known Product / Business" in product_df.columns:
            product_map = dict(
                zip(
                    product_df["Symbol"],
                    product_df["Commonly Known Product / Business"].fillna(""),
                )
            )
    stocks["common_product_business"] = stocks["symbol"].map(product_map).fillna("")
    stocks = add_display_symbol(stocks)
    stocks = add_trade_levels(stocks)

    # One shared price pull for the whole universe, reused by return metrics,
    # the 52-week-high scan and the drawer charts.
    universe_symbols = stocks["symbol"].dropna().unique().tolist()
    universe_prices = download_universe_prices(universe_symbols)
    stocks = add_return_metrics(stocks, prices=universe_prices)
    high52_info = compute_52w_high(universe_prices, universe_symbols)
    stocks = add_52w_high_flag(stocks, high52_info)

    # Tickertape (public API) fundamentals for the WHOLE universe so every stock
    # — not just the leading/breakout picks — carries P/E, P/B, ROE, ROCE,
    # margins, EPS, FCF, promoter holding, debt/equity and market cap. This
    # powers the per-metric "vs industry median" comparison in the drawer.
    stocks = add_tickertape_fundamentals(stocks)

    leading_industries = industries[
        (industries["stage"] == "stage_2")
        & (industries["rrg_quadrant"] == "leading")
    ].copy()
    leading_stocks = stocks[stocks["parent"].isin(leading_industries["name"])].copy()
    leading_stocks = add_yahoo_fundamentals(leading_stocks)
    # screener.in fills the gaps Yahoo leaves (ROE/ROCE/margins/debt/FCF/
    # promoter holding/quarterly growth) so the per-stock SWOT is complete.
    leading_stocks = add_screener_fundamentals(leading_stocks)
    # (Tickertape values are already on the base `stocks` and inherited by this
    # copy; Yahoo+screener above only fill the extra description/SWOT gaps.)
    leading_stocks = add_fundamental_scores(leading_stocks)
    leading_stocks = add_stock_notes(leading_stocks)

    # Stocks whose latest weekly candle closed above their 52-week high, given
    # the same fundamentals/financials/SWOT treatment as the leading stocks.
    # Yahoo/Screener caches are warm from the leading run, so overlapping
    # symbols are not re-fetched.
    breakout_stocks = stocks[stocks["weekly_close_above_52wh"]].copy()
    breakout_stocks = add_yahoo_fundamentals(breakout_stocks)
    breakout_stocks = add_screener_fundamentals(breakout_stocks)
    breakout_stocks = add_fundamental_scores(breakout_stocks)
    breakout_stocks = add_stock_notes(breakout_stocks)
    breakout_stocks = breakout_stocks.sort_values(
        ["stock_score", "market_cap_cr"], ascending=[False, False]
    )

    stocks = add_stock_notes(stocks)
    # Per-symbol chart files (~20yr weekly OHLC each) for the full universe so
    # any screener, breakout or backtest stock opens with a price chart in the
    # drawer. Written to dashboard/charts/ and lazy-loaded by the frontend on
    # drawer open, keeping dashboard_data.json small.
    chart_symbols_written = write_chart_files(universe_symbols)
    print(f"chart files written: {chart_symbols_written}/{len(universe_symbols)} -> {CHARTS_DIR}")

    # Product/business segment revenue breakups (screener.in) for the drawer pie.
    # Premium-gated: populated only when SCREENER_SESSION_COOKIE is set; otherwise
    # a graceful no-op and the drawer shows a "not disclosed" fallback.
    seg_display = (
        stocks["display_symbol"] if "display_symbol" in stocks.columns else stocks["symbol"]
    )
    segments_map = fetch_segments_map(
        stocks["symbol"].fillna("").tolist(),
        seg_display.fillna("").tolist(),
    )
    fundamental_picks = (
        leading_stocks.sort_values(["parent", "fundamental_score", "market_cap_cr"], ascending=[True, False, False])
        .groupby("parent", group_keys=False)
        .head(5)
    )

    metrics = {
        "industries_analyzed": int(len(industries)),
        "stage2_leading_industries": int(len(leading_industries)),
        "stocks_analyzed": int(len(stocks)),
        "stage2_leading_stocks": int(
            len(stocks[(stocks["stage"] == "stage_2") & (stocks["rrg_quadrant"] == "leading")])
        ),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stocks_above_52w_high": int(len(breakout_stocks)),
        "coverage_note": "Current data uses the latest completed Screener/Yahoo run. The last full industry scrape was rate-limited by Screener, so coverage is partial until the daily updater completes without rate-limit errors.",
        "trade_level_note": "CMP is the latest close. Entry is set at CMP. SL uses the tighter of the 30-week moving average when below CMP, or 8% below CMP. Target is 2R from entry.",
    }

    backtest = {"daily": {"summary": {}, "trades": [], "monthlyPnl": [], "monthlyCapital": [], "equityCurve": []}, "weekly": {"summary": {}, "trades": [], "monthlyPnl": [], "monthlyCapital": [], "equityCurve": []}}
    if BACKTEST_JSON.exists():
        try:
            backtest = json.loads(BACKTEST_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backtest = {"summary": {}, "trades": []}

    backtest_52w = {"summary": {}, "trades": [], "monthlyPnl": [], "monthlyCapital": [], "equityCurve": []}
    if BACKTEST_52W_JSON.exists():
        try:
            backtest_52w = json.loads(BACKTEST_52W_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    # --- Override headline ratios with Upstox fundamentals (VM-committed) ---
    # Upstox is authoritative for the ratios it exposes; a missing upstox_data.json
    # (e.g. before the VM's first commit) makes this a silent no-op.
    try:
        from upstox_sync.merge import apply_upstox_ratios_df, load_fundamentals
        _funds = load_fundamentals(DASHBOARD_DIR / "upstox_data.json")
        if _funds:
            _n = 0
            for _df in (stocks, leading_stocks, breakout_stocks, fundamental_picks):
                _n = max(_n, apply_upstox_ratios_df(_df, _funds))
            print(f"[upstox] overrode headline ratios for {_n} stocks from upstox_data.json")
    except Exception as _e:  # never let the Upstox merge break the core build
        print(f"[upstox] ratio override skipped: {_e}")

    payload = {
        "metrics": metrics,
        "industries": clean_records(industries),
        "leadingIndustries": clean_records(leading_industries),
        "stocks": clean_records(stocks),
        "leadingStocks": clean_records(leading_stocks),
        "breakout52wStocks": clean_records(breakout_stocks),
        "fundamentalPicks": clean_records(fundamental_picks),
        "industryTheses": INDUSTRY_THESES,
        # chartData moved to per-symbol files under dashboard/charts/ (lazy-loaded
        # on drawer open). Kept as an empty object for backward compatibility.
        "chartData": {},
        # {symbol: {segments:[{name,value,pct}], names:[...], gated, period}} for
        # the drawer revenue-breakup pie. Empty unless SCREENER_SESSION_COOKIE set.
        "segments": segments_map,
        "backtest": backtest,
        "backtest52w": backtest_52w,
    }
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(DATA_PATH)


if __name__ == "__main__":
    main()
