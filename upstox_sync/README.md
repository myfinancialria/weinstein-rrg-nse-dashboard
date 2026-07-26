# upstox_sync — Upstox fundamentals + options + FII/DII for the dashboard

Adds three Upstox v2 data families to the Weinstein/RRG dashboard, for the full
NSE universe, refreshed daily:

- **Fundamentals** — profile, key ratios (vs sector), income statement, balance
  sheet, cash flow, shareholding-over-time, corporate actions, peers.
- **Options** — PCR, max-pain, OI-by-strike per F&O underlying (current month) +
  NIFTY / BANK NIFTY.
- **FII / DII** — market-wide institutional flows by segment (cash, index/stock
  futures, index/stock options).

## Why it runs on the VM, not GitHub Actions

Upstox has **no headless login** — the daily OAuth token needs interactive 2FA, so
it cannot be refreshed inside the unattended Actions build. The sync therefore runs
on the **Oracle VM** (which already refreshes an Upstox token for the paper-trader /
stock360), writes `dashboard/upstox_data.json`, and pushes it. The Actions build and
Pages just consume that file.

```
VM ~20:00 IST:  upstox_sync  ->  dashboard/upstox_data.json  ->  git push
Actions 21:00 IST (scheduled):  build_dashboard_data.py reads it (overrides ratios)
                                 uploads dashboard/ -> Pages serves fresh data
```

Pushing `dashboard/*.json` does **not** self-trigger the workflow (`pages.yml`
path-ignores it); the scheduled run publishes it.

## How it plugs in

1. **Ratios (replace):** `build_dashboard_data.py` calls
   `upstox_sync.merge.apply_upstox_ratios_df()` right before writing
   `dashboard_data.json`, overriding each stock's P/E, P/B, ROE, ROCE, net/operating
   margin and promoter % with Upstox values (`fundamentals_source="upstox"`). Fields
   Upstox doesn't expose (EPS, D/E, FCF) keep their existing source. No-op if
   `upstox_data.json` is absent.
2. **Rich drawer sections (add):** the dashboard's stock drawer lazy-fetches
   `./upstox_data.json` once and renders an **Options** block (PCR/max-pain/OI-by-strike),
   full **financial statements**, **shareholding**, **corporate actions**, **peers**,
   and a market-wide **FII/DII** flows table.

## Split cadence

Options + FII/DII refresh every run; fundamentals (quarterly data) refresh only on
Saturdays or when older than `--max-age-days` (7), reusing the committed block
otherwise — saving ~2,400 calls/day.

## Usage (on the VM)

```bash
pip install -r requirements.txt          # requests already pinned
cp .env.example .env                      # add UPSTOX_API_KEY / SECRET / REDIRECT_URI

python -m upstox_sync login url            # daily token (expires ~03:30 IST)
python -m upstox_sync login "<redirect-url>"

python -m upstox_sync probe                # confirm entitlement of each API family
python -m upstox_sync one RELIANCE.NS      # build one symbol, print (test)
python -m upstox_sync sync                 # options + FII/DII (+ fundamentals if stale)
python -m upstox_sync sync --fundamentals  # force full fundamentals refresh
```

Deploy: `upstox_sync/deploy/run_sync.sh` (commit + push) on cron — see
`upstox_sync/deploy/crontab.example` (20:00 IST, Mon–Sat; Saturday forces
fundamentals).

## Layout

```
upstox_sync/
  client.py      Upstox v2 client (fundamentals + options + FII/DII + market)
  resolver.py    NSE instrument master → symbol⇄ISIN⇄instrument_key + F&O set
  build.py       fetch + assemble dashboard/upstox_data.json (split cadence)
  merge.py       override headline ratios on the stocks DataFrames (used by build_dashboard_data.py)
  login.py       Upstox OAuth (daily token)
  cli.py         login / probe / one / sync
  deploy/        run_sync.sh + crontab.example
```

## Notes / limits

- **F&O only** get options (~180 of the universe, detected from the instrument
  master's `underlying_key`); non-F&O stocks simply have no options block.
- **FII/DII is market-wide**, not per-stock (Upstox reports it by segment). It shows
  once in every drawer as market context.
- Tata Motors demerged — `TATAMOTORS` is aliased to `TMCV` in `resolver.py`.
- All fetches are defensive: one bad stock/section is skipped with a note, never
  sinking the run.
