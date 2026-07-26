"""Upstox data sync for the Weinstein/RRG dashboard.

Fetches, for the dashboard's NSE universe, three families from the Upstox v2 API:

  * Fundamentals  — profile, key ratios, income statement, balance sheet, cash flow,
    shareholding, corporate actions, peers (keyed by ISIN / instrument_key).
  * Options       — PCR, max-pain, OI-by-strike per F&O underlying (current month).
  * FII / DII      — market-wide institutional flows by segment (cash, futures, options).

It writes a single ``dashboard/upstox_data.json`` that (a) ``build_dashboard_data.py``
reads to override each stock's headline ratios, and (b) the dashboard front-end reads
lazily to render the rich drawer sections + a market-flows panel.

Runs on a host that holds a live Upstox token (the Oracle VM), NOT in GitHub Actions
— Upstox has no headless login, so the token cannot be refreshed unattended. The VM
commits the JSON; the Actions build just consumes it.
"""

__version__ = "0.1.0"
