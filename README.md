# Weinstein RRG NSE Dashboard

An NSE market dashboard for screening industries and stocks using Weinstein Stage 2, RRG leadership, technical filters, fundamentals, backtests, and a daily market blog.

## Features

- Weinstein Stage 2 and RRG-based industry and stock screening
- Fundamental scoring and best-stock filtering
- Daily and weekly backtest results
- Interactive dashboard with filters, sorting, charts, and stock detail view
- Pre-market / post-market blog with CPR levels, Nifty trend, global cues, and market probability view
- GitHub Pages workflow for publishing the dashboard as a webpage

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Yahoo Finance is the default data source and does not require credentials.

Optional `.env` values:

```text
FYERS_CLIENT_ID=your_fyers_client_id
FYERS_ACCESS_TOKEN=your_fyers_access_token
GIFT_NIFTY_LEVEL=
GIFT_NIFTY_SYMBOL=
```

## Run Locally

```bash
python3 run_dashboard_server.py
```

Open:

```text
http://127.0.0.1:8765/
```

## Refresh Data

```bash
./update_morning_market_blog.sh
```

For the full screener refresh:

```bash
./update_dashboard_daily.sh
```

## Publish

The GitHub Actions workflow in `.github/workflows/pages.yml` publishes the `dashboard/` folder to GitHub Pages.

It can run:

- on push to `main`
- on weekdays at 9:00 PM IST
- manually from the GitHub Actions tab

## Disclaimer

This project is for educational and research use only. It is not investment advice.
