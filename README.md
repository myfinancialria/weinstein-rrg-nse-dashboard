# Weinstein Stage 2 + RRG Daily Screener

This is a configurable daily NSE screener that pulls OHLCV history from Yahoo Finance by default, checks Weinstein Stage 2 conditions for sectors, sub-sectors, and stocks, and adds a 5-day RRG-style relative-strength tail versus a benchmark.

## Setup

```bash
cd /Users/nithin/Documents/Codex/2026-06-27/i/outputs/weinstein_rrg_screener
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

The default `config.yaml` uses:

```yaml
data_provider: yfinance
benchmark:
  symbol: ^NSEI
```

No credentials are needed for Yahoo Finance.

If you want to use FYERS instead, change `data_provider: fyers`, use FYERS-format symbols in `universe.csv`, and edit `.env`:

```bash
FYERS_CLIENT_ID=...
FYERS_ACCESS_TOKEN=...
```

`config.yaml` and `universe.csv` are already included with starter NSE examples. Edit `universe.csv` to add your full sector, sub-sector, and stock list.

## Universe Format

Use FYERS symbols exactly as accepted by the history API.

```csv
level,name,symbol,parent
sector,Nifty Bank,NSE:NIFTYBANK-INDEX,
subsector,Private Banks,NSE:NIFTYPVTBANK-INDEX,Nifty Bank
stock,HDFC Bank,NSE:HDFCBANK-EQ,Private Banks
```

Levels can be `sector`, `subsector`, or `stock`. The `parent` column lets the report preserve sector and sub-sector relationships.

## Run

```bash
python3 screener.py --config config.yaml
```

Reports are written to `reports/` as CSV and Markdown.

The Markdown report includes:

- `Candidates`: Stage 2 names in Leading or Improving RRG quadrants
- `Sectors Moving Toward Or Strengthening In Leading`: sector rows whose last 5 sessions show movement into Leading, Improving with rising RS-Ratio and RS-Momentum, or strengthening while already in Leading
- `Full Screener`: all sectors, sub-sectors, stocks, and any Yahoo data errors

## Daily Schedule

On macOS/Linux, add a cron entry after market data is available:

```cron
30 18 * * 1-5 cd /Users/nithin/Documents/Codex/2026-06-27/i/outputs/weinstein_rrg_screener && . .venv/bin/activate && python3 screener.py --config config.yaml
```

## Dashboard Website

The interactive dashboard is in `dashboard/index.html`. Run it locally with:

```bash
python3 run_dashboard_server.py
```

Then open:

```text
http://127.0.0.1:8765/
```

## GitHub Pages Publishing

This repo includes `.github/workflows/pages.yml`.

It publishes the `dashboard/` folder to GitHub Pages:

- on every push to `main`
- on weekdays at 9:00 PM IST (`15:30 UTC`)
- manually through GitHub Actions `workflow_dispatch`

The scheduled workflow refreshes `dashboard/dashboard_data.json` and
`dashboard/market_blog.json`, commits the refreshed JSON, and deploys the static
dashboard page.

For GIFT Nifty in the Market Blog tab, set one of these GitHub repository
secrets:

```text
GIFT_NIFTY_LEVEL=xxxxx
GIFT_NIFTY_SYMBOL=your_yahoo_proxy_symbol
```

If no GIFT Nifty input is configured, the blog still publishes using Nifty,
global indices, USD/INR, crude, gold, CPR, and trend data.

## Screening Logic

Weinstein Stage 2 checks:

- price above the 30-week moving average
- 30-week moving average rising over the last four weeks
- price above the 10-week moving average
- relative strength versus benchmark above its 10-week moving average
- price at least 75% of its 52-week high
- optional volume confirmation via `require_volume_above_ma`

RRG analysis:

- relative strength is computed as instrument close divided by benchmark close
- RS-Ratio is a z-score style normalization around 100
- RS-Momentum is normalized RS-Ratio change around 100
- the last five available trading days are included as a tail
- quadrants are `leading`, `weakening`, `lagging`, or `improving`

The RRG calculation is a practical screening approximation, not a licensed JdK RS-Ratio implementation.
