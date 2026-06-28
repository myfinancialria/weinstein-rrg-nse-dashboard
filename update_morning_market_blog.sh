#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

.venv/bin/python build_dashboard_data.py
.venv/bin/python generate_market_blog.py
