#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

: "${SCREENER_INDUSTRIES_WORKBOOK:?Set SCREENER_INDUSTRIES_WORKBOOK to the Screener industries workbook path.}"

.venv/bin/python analyze_screener_industries.py \
  --workbook "$SCREENER_INDUSTRIES_WORKBOOK" \
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
