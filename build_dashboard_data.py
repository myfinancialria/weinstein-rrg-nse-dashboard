from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from screener_fundamentals import add_screener_fundamentals


BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
DASHBOARD_DIR = BASE_DIR / "dashboard"
DATA_PATH = DASHBOARD_DIR / "dashboard_data.json"
FUNDAMENTALS_CACHE = REPORTS_DIR / "yahoo_fundamentals_cache.json"
BACKTEST_JSON = DASHBOARD_DIR / "backtest_results.json"
BACKTEST_52W_JSON = DASHBOARD_DIR / "backtest_52w_high.json"

INDUSTRY_CSV = REPORTS_DIR / "screener_industry_weinstein_rrg_2026-06-28.csv"
STOCK_CSV = REPORTS_DIR / "screener_industry_stock_rankings_2026-06-28.csv"
PRODUCT_XLSX = REPORTS_DIR / "stage2_rrg_leading_industries_best_stocks_products_2026-06-28.xlsx"


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


def build_chart_data(symbols: list[str], prices: pd.DataFrame | None = None) -> dict[str, list[dict]]:
    """Compact daily close series (time + close only - all the drawer line
    chart reads) for every symbol, reusing the shared price pull when given."""
    if not symbols:
        return {}
    if prices is None:
        prices = download_universe_prices(symbols)
    if prices is None or prices.empty:
        return {}

    chart_data: dict[str, list[dict]] = {}
    for symbol in symbols:
        close = _symbol_series(prices, "Close", symbol)
        if close is None or close.empty:
            chart_data[symbol] = []
            continue
        tail = close.tail(230)
        chart_data[symbol] = [
            {"time": idx.strftime("%Y-%m-%d"), "close": round(float(value), 2)}
            for idx, value in tail.items()
        ]
    return chart_data


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

    leading_industries = industries[
        (industries["stage"] == "stage_2")
        & (industries["rrg_quadrant"] == "leading")
    ].copy()
    leading_stocks = stocks[stocks["parent"].isin(leading_industries["name"])].copy()
    leading_stocks = add_yahoo_fundamentals(leading_stocks)
    # screener.in fills the gaps Yahoo leaves (ROE/ROCE/margins/debt/FCF/
    # promoter holding/quarterly growth) so the per-stock SWOT is complete.
    leading_stocks = add_screener_fundamentals(leading_stocks)
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
    # Charts for the full universe so any screener, breakout or backtest stock
    # opens with a price chart in the drawer.
    chart_data = build_chart_data(universe_symbols, prices=universe_prices)
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

    payload = {
        "metrics": metrics,
        "industries": clean_records(industries),
        "leadingIndustries": clean_records(leading_industries),
        "stocks": clean_records(stocks),
        "leadingStocks": clean_records(leading_stocks),
        "breakout52wStocks": clean_records(breakout_stocks),
        "fundamentalPicks": clean_records(fundamental_picks),
        "industryTheses": INDUSTRY_THESES,
        "chartData": chart_data,
        "backtest": backtest,
        "backtest52w": backtest_52w,
    }
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(DATA_PATH)


if __name__ == "__main__":
    main()
