#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

.venv/bin/python analyze_screener_industries.py \
  --workbook /Users/nithin/Documents/Codex/2026-06-28/dow/outputs/screener_industries/screener_industries_nse_details.xlsx \
  --config config.yaml \
  --output-dir reports \
  --sleep 1.5 \
  --retries 5 \
  --backoff 10

.venv/bin/python create_stage2_leading_workbook.py
.venv/bin/python add_common_products_to_workbook.py
.venv/bin/python backtest_strategy.py
.venv/bin/python build_dashboard_data.py
.venv/bin/python generate_market_blog.py
