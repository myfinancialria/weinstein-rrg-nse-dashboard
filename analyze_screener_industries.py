from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

from screener import analyze_rrg, analyze_weinstein, load_config, write_markdown_report


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


@dataclass(frozen=True)
class IndustryMember:
    industry: str
    sector: str
    nse_industry: str
    url: str
    name: str
    screener_symbol: str
    yahoo_symbol: str
    market_cap_cr: float
    dividend_yield_pct: float
    np_qtr_cr: float
    qtr_profit_var_pct: float
    sales_qtr_cr: float
    qtr_sales_var_pct: float
    pe: float
    roce: float


def parse_float(value: str) -> float:
    cleaned = value.replace(",", "").replace("%", "").strip()
    if cleaned in {"", "-", "None"}:
        return np.nan
    try:
        return float(cleaned)
    except ValueError:
        return np.nan


def symbol_from_href(href: str) -> str:
    match = re.search(r"/company/([^/]+)/?", href)
    return match.group(1) if match else ""


def to_yahoo_symbol(screener_symbol: str) -> str:
    if not screener_symbol or screener_symbol.isdigit():
        return ""
    return f"{screener_symbol}.NS"


def scrape_industry_members(
    row: pd.Series,
    max_members: int,
    sleep_s: float,
    retries: int,
    backoff_s: float,
) -> list[IndustryMember]:
    url = str(row["Screener Industry URL"])
    response = None
    for attempt in range(retries + 1):
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25)
        if response.status_code != 429:
            break
        wait_s = backoff_s * (attempt + 1)
        time.sleep(wait_s)
    assert response is not None
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    members: list[IndustryMember] = []
    for tr in soup.select("table tr"):
        link = tr.select_one('a[href^="/company/"]')
        if link is None:
            continue
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
        if len(cells) < 10:
            continue
        screener_symbol = symbol_from_href(link.get("href", ""))
        yahoo_symbol = to_yahoo_symbol(screener_symbol)
        if not yahoo_symbol:
            continue
        members.append(
            IndustryMember(
                industry=str(row["Screener Industry"]),
                sector=str(row["Sector"]),
                nse_industry=str(row["NSE Industry"]),
                url=url,
                name=cells[1],
                screener_symbol=screener_symbol,
                yahoo_symbol=yahoo_symbol,
                pe=parse_float(cells[3]),
                market_cap_cr=parse_float(cells[4]),
                dividend_yield_pct=parse_float(cells[5]) if len(cells) > 5 else np.nan,
                np_qtr_cr=parse_float(cells[6]) if len(cells) > 6 else np.nan,
                qtr_profit_var_pct=parse_float(cells[7]) if len(cells) > 7 else np.nan,
                sales_qtr_cr=parse_float(cells[8]) if len(cells) > 8 else np.nan,
                qtr_sales_var_pct=parse_float(cells[9]) if len(cells) > 9 else np.nan,
                roce=parse_float(cells[10]) if len(cells) > 10 else np.nan,
            )
        )
    time.sleep(sleep_s)
    members = sorted(
        members,
        key=lambda member: -1 if pd.isna(member.market_cap_cr) else member.market_cap_cr,
        reverse=True,
    )
    return members[:max_members]


def history_to_ohlcv(close: pd.Series) -> pd.DataFrame:
    close = close.dropna()
    return pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 0.0,
        },
        index=pd.to_datetime(close.index).tz_localize(None),
    )


def download_prices(symbols: list[str], start: date, end: date) -> pd.DataFrame:
    data = yf.download(
        sorted(set(symbols)),
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=True,
        group_by="column",
    )
    if data.empty:
        raise RuntimeError("Yahoo Finance returned no prices.")
    if isinstance(data.columns, pd.MultiIndex):
        if "Close" in data.columns.get_level_values(0):
            close = data["Close"]
        else:
            close = data.xs("Close", axis=1, level=1)
    else:
        close = data[["Close"]].rename(columns={"Close": symbols[0]})
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.dropna(how="all")


def make_industry_composite(close: pd.DataFrame, symbols: list[str], min_members: int) -> pd.Series:
    available = [symbol for symbol in symbols if symbol in close.columns]
    if len(available) < min_members:
        return pd.Series(dtype=float)
    normalized = close[available].ffill().dropna(how="all")
    normalized = normalized.dropna(axis=1, thresh=max(80, int(len(normalized) * 0.55)))
    if normalized.shape[1] < min_members:
        return pd.Series(dtype=float)
    normalized = normalized / normalized.iloc[0] * 100
    return normalized.mean(axis=1).dropna()


def score_stock(row: pd.Series) -> float:
    score = 0.0
    score += float(row.get("stage2_score", 0)) * 20
    score += 25 if row.get("stage") == "stage_2" else 0
    score += 20 if row.get("rrg_quadrant") == "leading" else 0
    score += 10 if row.get("moving_to_leading") else 0
    score += max(0.0, float(row.get("rs_ratio", 100) or 100) - 100) * 5
    score += max(0.0, float(row.get("rs_momentum", 100) or 100) - 100) * 5
    score += max(0.0, float(row.get("weekly_ma_slope_4w_pct", 0) or 0)) * 2
    return round(score, 2)


def run(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    workbook_path = Path(args.workbook).expanduser().resolve()
    config = load_config(Path(args.config).expanduser().resolve())
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    industries = pd.read_excel(workbook_path, sheet_name="Industries")
    if args.limit:
        industries = industries.head(args.limit)

    all_members: list[IndustryMember] = []
    errors: list[dict[str, Any]] = []
    for _, row in industries.iterrows():
        try:
            members = scrape_industry_members(
                row,
                args.max_members,
                args.sleep,
                args.retries,
                args.backoff,
            )
            all_members.extend(members)
        except Exception as exc:
            errors.append(
                {
                    "industry": row.get("Screener Industry"),
                    "url": row.get("Screener Industry URL"),
                    "error": str(exc),
                }
            )

    members_df = pd.DataFrame([member.__dict__ for member in all_members])
    if members_df.empty:
        raise RuntimeError("No industry members were scraped from Screener.")

    end = date.today()
    start = end - timedelta(days=int(config["history"]["lookback_days"]))
    benchmark_close = download_prices([config["benchmark"]["symbol"]], start, end).iloc[:, 0]
    benchmark_daily = history_to_ohlcv(benchmark_close)
    all_symbols = members_df["yahoo_symbol"].dropna().unique().tolist()
    close = download_prices(all_symbols, start, end)

    industry_rows: list[dict[str, Any]] = []
    stock_rows: list[dict[str, Any]] = []
    for industry, group in members_df.groupby("industry"):
        symbols = group.sort_values("market_cap_cr", ascending=False)["yahoo_symbol"].tolist()
        composite = make_industry_composite(close, symbols, args.min_members)
        if composite.empty:
            continue
        daily = history_to_ohlcv(composite)
        industry_rows.append(
            {
                "level": "industry",
                "name": industry,
                "symbol": "equal_weight_top_members",
                "parent": group["sector"].iloc[0],
                "nse_industry": group["nse_industry"].iloc[0],
                "member_count": len(symbols),
                "source_url": group["url"].iloc[0],
                "error": "",
                **analyze_weinstein(daily, benchmark_daily, config),
                **analyze_rrg(daily, benchmark_daily, config),
            }
        )

        for member in group.itertuples(index=False):
            if member.yahoo_symbol not in close.columns:
                continue
            stock_daily = history_to_ohlcv(close[member.yahoo_symbol])
            try:
                stock_rows.append(
                    {
                        "level": "stock",
                        "name": member.name,
                        "symbol": member.yahoo_symbol,
                        "parent": industry,
                        "sector": member.sector,
                        "market_cap_cr": member.market_cap_cr,
                        "dividend_yield_pct": member.dividend_yield_pct,
                        "np_qtr_cr": member.np_qtr_cr,
                        "qtr_profit_var_pct": member.qtr_profit_var_pct,
                        "sales_qtr_cr": member.sales_qtr_cr,
                        "qtr_sales_var_pct": member.qtr_sales_var_pct,
                        "pe": member.pe,
                        "roce": member.roce,
                        "error": "",
                        **analyze_weinstein(stock_daily, benchmark_daily, config),
                        **analyze_rrg(stock_daily, benchmark_daily, config),
                    }
                )
            except Exception as exc:
                stock_rows.append(
                    {
                        "level": "stock",
                        "name": member.name,
                        "symbol": member.yahoo_symbol,
                        "parent": industry,
                        "sector": member.sector,
                        "market_cap_cr": member.market_cap_cr,
                        "dividend_yield_pct": member.dividend_yield_pct,
                        "np_qtr_cr": member.np_qtr_cr,
                        "qtr_profit_var_pct": member.qtr_profit_var_pct,
                        "sales_qtr_cr": member.sales_qtr_cr,
                        "qtr_sales_var_pct": member.qtr_sales_var_pct,
                        "pe": member.pe,
                        "roce": member.roce,
                        "stage": "data_error",
                        "stage2_score": 0,
                        "rrg_quadrant": "data_error",
                        "rrg_quadrant_5d_ago": "data_error",
                        "error": str(exc),
                    }
                )

    industry_report = pd.DataFrame(industry_rows)
    stock_report = pd.DataFrame(stock_rows)
    industry_report["just_entered_leading"] = (
        (industry_report["stage"] == "stage_2")
        & (industry_report["rrg_quadrant"] == "leading")
        & (industry_report["rrg_quadrant_5d_ago"] != "leading")
        & (industry_report["rs_ratio_5d_delta"] > 0)
        & (industry_report["rs_momentum_5d_delta"] > 0)
    )
    if not stock_report.empty:
        stock_report["stock_score"] = stock_report.apply(score_stock, axis=1)

    stamp = date.today().isoformat()
    industry_csv = output_dir / f"screener_industry_weinstein_rrg_{stamp}.csv"
    stock_csv = output_dir / f"screener_industry_stock_rankings_{stamp}.csv"
    summary_md = output_dir / f"screener_industry_stage2_rrg_summary_{stamp}.md"
    industry_report.to_csv(industry_csv, index=False)
    stock_report.to_csv(stock_csv, index=False)

    qualifying = industry_report[industry_report["just_entered_leading"]].sort_values(
        ["rs_momentum", "rs_ratio"], ascending=False
    )
    lines = [
        f"# Screener Industries: Weinstein Stage 2 + Newly Leading RRG ({stamp})",
        "",
        f"Reference workbook: {workbook_path}",
        f"Benchmark: {config['benchmark']['name']} ({config['benchmark']['symbol']})",
        "",
        "Industry composites are equal-weighted from the largest available Yahoo Finance members scraped from each Screener industry page.",
        "",
        "## Industries In Stage 2 That Just Entered Leading",
        "",
    ]
    if qualifying.empty:
        lines.append("No industry met all filters: Stage 2, current RRG Leading, and 5-day-ago quadrant not Leading with rising RS-Ratio and RS-Momentum.")
    else:
        lines.append(
            qualifying[
                [
                    "name",
                    "parent",
                    "nse_industry",
                    "member_count",
                    "stage2_score",
                    "rrg_quadrant_5d_ago",
                    "rrg_quadrant",
                    "rs_ratio",
                    "rs_momentum",
                    "rs_ratio_5d_delta",
                    "rs_momentum_5d_delta",
                    "source_url",
                ]
            ].to_markdown(index=False)
        )
        for industry in qualifying["name"]:
            ranked = stock_report[stock_report["parent"] == industry].sort_values(
                ["stock_score", "market_cap_cr"], ascending=False
            ).head(args.best_stocks)
            lines.extend(["", f"### Best Stocks: {industry}", ""])
            lines.append(
                ranked[
                    [
                        "name",
                        "symbol",
                        "stage",
                        "stage2_score",
                        "rrg_quadrant_5d_ago",
                        "rrg_quadrant",
                        "rs_ratio",
                        "rs_momentum",
                        "weekly_ma_slope_4w_pct",
                        "market_cap_cr",
                        "pe",
                        "roce",
                        "stock_score",
                    ]
                ].to_markdown(index=False)
            )

    if errors:
        lines.extend(["", "## Scrape Errors", ""])
        lines.append(pd.DataFrame(errors).to_markdown(index=False))

    summary_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {industry_csv}")
    print(f"Wrote {stock_csv}")
    print(f"Wrote {summary_md}")
    return industry_csv, stock_csv, summary_md


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--max-members", type=int, default=10)
    parser.add_argument("--min-members", type=int, default=3)
    parser.add_argument("--best-stocks", type=int, default=7)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=4.0)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
