# India Equity Database

Survivorship-bias-free database of NSE top-500 companies by market cap, quarterly rebalanced, covering Mar 2018 onwards. Outputs long-format CSVs for prices, fundamentals, shares outstanding, and universe history with free-float columns.

---

## Setup

### Prerequisites

- Python 3.10+
- Internet access (NSE, Yahoo Finance, screener.in)

```bash
pip install -r requirements.txt
```

### Configuration (`pipeline/configs/config.py`)

All tuneable constants live in `pipeline/configs/config.py`. The most commonly changed settings:

```python
PROJECT_START_DATE = "2018-06-30"  # hard floor for all history; do not lower
UNIVERSE_SIZE  = 500               # top-N by market cap per quarter
SCREENER_CACHE_DAYS = 7            # re-download screener HTML if older than this
XBRL_INDEX_CACHE_DAYS = 1          # re-fetch NSE filings index if older than this

MIN_SLEEP_NSE,      MAX_SLEEP_NSE      = 1.0, 2.0   # seconds between NSE calls (per thread)
MIN_SLEEP_SCREENER, MAX_SLEEP_SCREENER = 2.0, 4.0   # seconds between screener calls (per thread)
MAX_RETRIES = 3

XBRL_WORKERS = 8           # parallel threads for fetch_shp_xbrl.py
FINANCIALS_WORKERS = 8     # parallel threads for fetch_financials.py
```

Workers cap practical parallelism. Per-thread `polite_sleep` does *not* throttle aggregate request rate — peak concurrent requests to NSE is bounded by `XBRL_WORKERS`. NSE/Akamai typically tolerate ~8–16 concurrent connections per IP before 403/429; pushing higher buys nothing without rotating IPs.

`PROJECT_START_DATE` is the single source of truth for history depth across the entire pipeline. All fetchers and the universe ranker respect it. Do not lower it without re-validating XBRL coverage for the new range.

---

## Running the Pipeline

### Quick Start — Smoke Test (3 symbols)

Run this first to verify everything works before committing to a full 500-symbol pull.

```bash
# Step 1: Seed metadata (bootstrap — no ranking yet)
python -m pipeline.universe --bootstrap --refresh-universe --symbols RELIANCE,TCS,ZYDUSWELL

# Step 2: Fetch shares outstanding from NSE SHP XBRL filings
python -m pipeline.fetch_shp_xbrl --symbols RELIANCE,TCS,ZYDUSWELL

# Step 3: Rank universe using NSE bhavcopy prices × XBRL shares
python -m pipeline.universe --symbols RELIANCE,TCS,ZYDUSWELL

# Step 4: Fetch quarterly/annual financials from screener.in
python -m pipeline.fetch_financials --symbols RELIANCE,TCS,ZYDUSWELL

# Step 5: Fetch adjusted prices for total-return series
python -m pipeline.fetch_prices --symbols RELIANCE,TCS,ZYDUSWELL

# Step 6: Validate
python -m pipeline.validate
```

Expected outcome: `validate.py` exits with warnings about universe size (expected — only 3 symbols), but no structural failures. All 6 scripts complete without errors. Verify the RELIANCE Mar-2018 row in `shares_outstanding.csv`: `total_shares=6334651022`, `promoter_shares=2926202148`, `free_float_shares=3408448874`.

### Full Run

The pipeline has a fixed execution order. `universe.py` needs `shares_outstanding.csv` from `fetch_shp_xbrl.py` before it can rank; `fetch_shp_xbrl.py` needs `metadata.csv` from `universe.py --bootstrap`.

`universe.py --bootstrap --refresh-universe` triggers `build_symbol_history_from_bhavcopies()`, which scans cached bhavcopy parquets to discover historically-traded symbols. If `bhavcopy_cache/` is empty, the bootstrap auto-warms it by fetching quarter-end snapshots across `PROJECT_START_DATE..today` before scanning — adds ~5–10 min on a cold first run; subsequent runs are < 1 min.

```bash
# Step 1: Seed metadata with all symbols (no ranking)
python -m pipeline.universe --bootstrap --refresh-universe

# Step 2: Fetch shares outstanding from NSE SHP XBRL filings
python -m pipeline.fetch_shp_xbrl

# Step 3: Rank universe using NSE bhavcopy × XBRL shares; rebuilds metadata.csv to universe symbols only
python -m pipeline.universe

# Step 4: Fetch financials from screener.in (quarterly + annual P&L / BS / CF)
python -m pipeline.fetch_financials

# Step 5: Fetch adjusted prices (yfinance, for total-return series)
python -m pipeline.fetch_prices

# Step 6: Validate
python -m pipeline.validate
```

**Important**: step 3 (universe ranking) must run before step 4 (fetch_financials). `universe.py` narrows `metadata.csv` from the broad bootstrap set to universe-only symbols; `fetch_financials.py` reads `metadata.csv` to determine which symbols to scrape. Running them in the wrong order causes financials to be fetched for non-universe symbols.

Approximate runtimes (500 symbols, direct connection):

| Script | Time |
|---|---|
| `universe.py --bootstrap` | 5–10 min on cold `bhavcopy_cache/` (auto-warms it); < 1 min thereafter |
| `fetch_shp_xbrl.py` | 45–90 min (first run, 4 workers — ~15K XBRL downloads; cached on re-run) |
| `universe.py` (ranking) | 5–10 min (33 bhavcopy fetches, cached after first run) |
| `fetch_financials.py` | 45–60 min |
| `fetch_prices.py` | 5–10 min yfinance batch + ~15–30 min cold bhavcopy fallback per delisted symbol (cached after first run) |
| `validate.py` | < 1 min |

### Incremental Updates

`fetch_shp_xbrl.py` only downloads filings not already on disk and checks the filings index cache (1-day TTL). `fetch_financials.py` skips symbols with fresh cached HTML. `fetch_prices.py` does a single batch `yf.download` and overwrites `prices_panel.csv` each run. Bhavcopy parquets are cached permanently.

```bash
# Run after any trading day to keep prices and SHP filings current
python -m pipeline.fetch_shp_xbrl
python -m pipeline.fetch_prices
python -m pipeline.validate

# Quarterly refresh — recommended order
python -m pipeline.universe --bootstrap --refresh-universe   # refresh symbol list
python -m pipeline.fetch_shp_xbrl                           # pick up any new quarterly filings
python -m pipeline.universe                                  # re-rank with updated shares; narrows metadata
python -m pipeline.fetch_financials                          # refresh financials (runs against ranked metadata)
python -m pipeline.fetch_prices                              # refresh adjusted prices
python -m pipeline.validate
```

To force a full re-fetch:

```bash
# Re-download all XBRL files (ignore disk cache)
python -m pipeline.fetch_shp_xbrl --force

# Re-download all screener HTML
python -m pipeline.fetch_financials --force

# Re-build universe from scratch (also re-seeds metadata)
python -m pipeline.universe --bootstrap --refresh-universe
```

### CLI Flags

| Flag | Scripts | Description |
|---|---|---|
| `--symbols A,B,C` | all | Run only these NSE symbols (comma-separated, no spaces) |
| `--bootstrap` | `universe.py` | Write flat metadata.csv with all symbols (no ranking). Must run before `fetch_shp_xbrl.py` on a fresh build. |
| `--refresh-universe` | `universe.py` | Force re-download of raw NSE equity list |
| `--force` | `fetch_shp_xbrl.py`, `fetch_financials.py` | Ignore cache; re-fetch all XBRL files / screener HTML from scratch |
| `--start-date YYYY-MM-DD` | `universe.py` | Override start date for universe history (must be ≥ `PROJECT_START_DATE`) |
| `--start-date YYYY-MM-DD` | `fetch_shp_xbrl.py` | Skip filings before this date (must be ≥ `PROJECT_START_DATE`) |
| `--start-date YYYY-MM-DD` | `fetch_prices.py` | Override fetch start date (must be ≥ `PROJECT_START_DATE`) |
| `--workers N` | `fetch_shp_xbrl.py`, `fetch_financials.py` | Override parallel worker threads (defaults from `config.XBRL_WORKERS` / `config.FINANCIALS_WORKERS`, both 8; safe band is 8–16, beyond that NSE/Akamai throttle) |
| `--no-fallback` | `fetch_prices.py` | Skip bhavcopy fallback for symbols yfinance returns empty (yfinance-only mode) |

All `--start-date` overrides may move the effective start **later** than `PROJECT_START_DATE` but never earlier. Each script rejects an earlier value with a clear error.

### Lineage CLI

```bash
python -m pipeline.utils.lineage --symbol TATAMOTORS   # show demerger + rename events
python -m pipeline.utils.lineage --symbol HDFCBANK     # show HDFC merger incoming event
python -m pipeline.utils.lineage --symbol RELIANCE     # show JIOFIN spinoff outgoing event
```

For backtesting on top of this dataset, see [../../backtest/docs/README.md](../../backtest/docs/README.md).

---

## Output Files

All files written to `pipeline/data/`. Panels use **long format**: `date, symbol, parameter, value`.

| File | Description |
|---|---|
| `metadata.csv` | Master symbol list — one row per unique ticker ever in top-500 across all quarters. After `--bootstrap` contains all NSE symbols; after `universe.py` ranking, narrowed to universe symbols only (including delisted tickers that appeared historically). |
| `universe_history.csv` | Point-in-time top-500 ranking per quarter. Columns: `quarter_end_date, rank, symbol, isin, market_cap_inr, free_float_market_cap_inr, close_inr, shares_outstanding, free_float_shares`. Computed from NSE bhavcopy unadjusted close × XBRL shares. |
| `raw_universe.csv` | Full union of all symbols ever known: live NSE list + Wikipedia + bhavcopy history. Never shrinks — once a symbol is added it is never removed. |
| `symbol_history.csv` | Every NSE EQ symbol seen in any cached bhavcopy parquet. Columns: `symbol, first_seen_date, last_seen_date, status, isin, source`. `last_seen_date` is blank for symbols still active in the most recent cached parquet. |
| `corporate_actions.csv` | Manually-curated audit trail: renames, demergers, mergers, delistings. Columns: `event_date, event_type, predecessor_symbols, successor_symbols, predecessor_isins, successor_isins, ratio, source_url, notes`. Consumed by `validate.py` and `utils/lineage.py`; **not used by ranking code**. |
| `data_gaps.csv` | Unified failure log. Every fetcher appends one row per unrecoverable symbol via `pipeline.utils.gaps.log_gap()`. Columns: `script, symbol, reason, error_msg, recorded_date`. Reason codes include `xbrl_unavailable_for_delisted`, `bhavcopy_no_rows_in_range`, `no_corp_actions_for_fallback`, `PageFetchFailed`, `ParseFailed`, `NoRecentData`, `NoQuarterlyData`. |
| `corporate_action_adjustments.csv` | Every split/bonus/dividend applied to a bhavcopy-fallback symbol. Columns: `symbol, ex_date, action_type, ratio_num, ratio_denom, amount, source, raw_subject`. Sourced from NSE `/api/corporates-corporateActions`. Consumed by `validate.py`; not used by ranking. |
| `nse_corpact_cache/{SYMBOL}.json` | Raw NSE corporate-action API response per symbol (7-day TTL). |
| `shares_outstanding.csv` | Per-filing shares data sourced from NSE SHP XBRL. Columns: `symbol, date, total_shares, promoter_shares, free_float_shares, source`. Multiple filings per symbol (quarterly cadence + material-change filings). Written by `fetch_shp_xbrl.py`. |
| `xbrl_cache/{SYMBOL}/_index.json` | Cached NSE filings index per symbol (1-day TTL). |
| `xbrl_cache/{SYMBOL}/{YYYY-MM-DD}_{recordId}.xml` | Raw XBRL files, cached permanently. |
| `bhavcopy_cache/{YYYYMMDD}.parquet` | Daily EQ bhavcopy snapshots cached as parquet. Fetched once per trading day; re-used on subsequent runs. |
| `prices_panel.csv` | Daily OHLCV, long format. `parameter` ∈ {open, high, low, close, volume}. Primary source: yfinance `auto_adjust=True`. Fallback for yfinance-empty symbols (typically merged/delisted, e.g. HDFC): daily NSE bhavcopy back-adjusted with splits/bonuses/dividends from NSE corporate-actions API (matches yfinance auto-adjust semantics). Mixed-source panel; per-symbol provenance is implicit via `corporate_action_adjustments.csv`. |
| `financials_panel.csv` | Quarterly P&L from screener.in. `date` = quarter-end (Mar 31 / Jun 30 / Sep 30 / Dec 31). Rows filtered to `date >= PROJECT_START_DATE`. Banks include `financing_profit`, `financing_margin`, `gross_npa`, `net_npa` instead of `ebitda`/`ebit`. |
| `financials_annual_panel.csv` | Annual BS + CF. `date` = 31-Mar-YYYY (Indian FY end). Rows filtered to `date >= PROJECT_START_DATE`. Banks include `deposits` in BS. |
| `validation_report.txt` | Last validation run output |
| `screener_cache/{SYMBOL}.html` | Cached screener.in HTML (refreshed every 7 days) |

All paths above are relative to `pipeline/data/`.

### Reading panels in Python

```python
import pandas as pd

# Prices
prices = pd.read_csv("pipeline/data/prices_panel.csv", parse_dates=["date"])
# Filter: RELIANCE close prices
reliance_close = prices[(prices["symbol"] == "RELIANCE") & (prices["parameter"] == "close")]

# Pivot to wide (date x symbol) for a single parameter
close_wide = prices[prices["parameter"] == "close"].pivot(
    index="date", columns="symbol", values="value"
)

# Quarterly financials
fin = pd.read_csv("pipeline/data/financials_panel.csv", parse_dates=["date"])
# Filter: TCS sales by quarter
tcs_sales = fin[(fin["symbol"] == "TCS") & (fin["parameter"] == "sales")]

# Annual financials
ann = pd.read_csv("pipeline/data/financials_annual_panel.csv", parse_dates=["date"])

# Shares outstanding (XBRL-sourced)
shares = pd.read_csv("pipeline/data/shares_outstanding.csv", parse_dates=["date"])
reliance_shares = shares[shares["symbol"] == "RELIANCE"].sort_values("date")
```

---

## Validation

### Output

```
CHECK                                            STATUS DETAIL
-----------------------------------------------------------------------------------------------
shares_integrity_sum                             PASS   0 rows where promoter+free_float != total
shares_integrity_bounds                          PASS   0 rows with out-of-bounds values
universe_quarter_count                           PASS   33 quarters
universe_size_per_quarter                        PASS   min=498 max=500
universe_no_duplicates                           PASS   0 duplicates
metadata_symbol_count                            PASS   1022 symbols
price_symbols_coverage                           PASS   100.0% present; 0 missing
price_fields_per_symbol                          PASS   0 symbols with != 5 fields
price_no_duplicate_rows                          PASS   0 duplicate rows
price_close_coverage_80pct                       PASS   0 symbols below 80%
price_no_negatives                               PASS   0 negative price values
price_ohlc_sanity                                PASS   0 OHLC violations
price_data_staleness                             PASS   Last date 2026-05-22 (1d ago)
financials_symbol_coverage                       PASS   0 symbols with no data
financials_quarter_alignment                     PASS   100.0% of dates in Mar/Jun/Sep/Dec
financials_core_line_items                       PASS   0 symbols missing core items
financials_no_future_dates                       PASS   0 future-dated rows
cross_universe_in_both_panels                    PASS   0 universe symbols in NEITHER panel
failed_financials_ratio                          PASS   5.1% (25/1022)
-----------------------------------------------------------------------------------------------
OVERALL: PASS (0 warnings, 0 failures, 19 passed)
```

Exit code 0 = PASS (warnings ok). Exit code 1 = one or more FAIL checks.

### Known limitations of the smoke test (3 symbols)

When running `--symbols RELIANCE,TCS,ZYDUSWELL`, the following FAILs are expected and not bugs:

| Check | Why it fails in smoke test |
|---|---|
| `universe_size_per_quarter` | Only 3 symbols; needs 490+ for PASS |
| `financials_quarter_coverage_40q` | Screener HTML shows only the last 13 quarters |

### Banking / NBFC parameters

Banks and NBFCs (HDFCBANK, KOTAKBANK, SBIN, BAJFINANCE, etc.) produce a different parameter set than manufacturing companies:

| What changes | Detail |
|---|---|
| `ebitda`, `ebit` absent | Not meaningful for financial companies; no PASS/FAIL impact — validate checks for `ebitda` OR `financing_profit` |
| `financing_profit` | NII (Net Interest Income) — replaces EBITDA as the core margin metric |
| `financing_margin` | NIM % |
| `gross_npa`, `net_npa` | NPA quality ratios (quarterly only) |
| `deposits` | Core funding metric in annual balance sheet |

---

## Logs

Each script run appends to a timestamped log in `pipeline/logs/run_YYYYMMDD_HHMMSS.log`.

```
pipeline/logs/
  run_20260517_163941.log
  run_20260517_163956.log
  ...
```

Log levels:
- `INFO` — per-symbol progress
- `WARN` — retries, non-fatal issues
- `ERROR` — final failures (symbol skipped, written to failed CSVs)
- `DEBUG` — sleep duration, cache hits (file only, not printed to console)

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Yahoo API requires curl_cffi session` | Passing a `requests.Session` into yfinance | Don't pass a session — yfinance >= 0.2.40 manages its own. `fetch_prices.py` uses bare `yf.download` |
| High rate of 429s from screener.in | Too fast or IP flagged | Increase `MIN_SLEEP_SCREENER`; back off for a few hours if persistent |
| NSE bhavcopy returns 403 / rate-limited | Too many requests to NSE archives | Increase `MIN_SLEEP_NSE`; a fresh `curl_cffi` session (hitting nseindia.com first) is seeded automatically |
| `FileNotFoundError` for a quarter-end date | That date is a holiday — no bhavcopy exists | Expected; `universe.py` walks back up to 7 days to find the nearest trading day |
| `universe_history is empty` | `shares_outstanding.csv` is missing or empty | Run `fetch_shp_xbrl.py` first (step 2) before running `universe.py` without `--bootstrap` |
| `fetch_shp_xbrl.py` returns empty index for a symbol | NSE warmup not completed or session expired | Each worker thread warms up once on first use and reuses the `curl_cffi` session for all its symbols; if persistent, delete `pipeline/data/xbrl_cache/{SYMBOL}/_index.json` and re-run |
| XBRL parse returns None for all filings | XML files are error pages (< 5 KB) | Re-run with `--force` to re-download; check NSE site for outages |
| `--start-date` rejected with error | Requested date is before `PROJECT_START_DATE` | XBRL coverage only starts from `PROJECT_START_DATE`; choose a date on or after that |
| Encoding errors on Windows console | `→` or `…` in log strings | Fixed — logger stream uses `errors="replace"` |
| `File is not a zip file` when parsing screener | XLSX export requires login; you got HTML back | Expected — the script uses HTML parsing, not XLSX |

---

## Known Limitations

| Limitation | Detail |
|---|---|
| Pre-2018 history | Bound by `PROJECT_START_DATE` and earliest XBRL availability. |
| Delisted symbols with no XBRL | If NSE's XBRL API does not serve filings for a delisted symbol, it appears in `symbol_history.csv` and `raw_universe.csv` but is absent from `universe_history.csv`. Recorded in `pipeline/data/data_gaps.csv` with `reason=xbrl_unavailable_for_delisted`. |
| No return splicing | `fetch_prices.py` is unchanged. Analysts wanting a continuous TATAMOTORS→TMPV return series must join via `corporate_actions.csv` manually. `python -m pipeline.utils.lineage --symbol TATAMOTORS` shows the link. |
| Free-float reconciliation post-demerger | TMPV + TMCV free-float sum ≠ pre-demerger TATAMOTORS free-float. Correct — reflects corporate-action mechanics; do not reconcile. |
| Symbol reincarnation | If a delisted ticker is reused by an unrelated company later, `symbol_history.csv` uses a single `(first_seen, last_seen)` pair which collapses both into one row. Validator's unexplained-entry checks will surface it for manual review. |
| Same-symbol-different-ISIN events | Face-value splits with ISIN reassignment pass through transparently (nothing joins on ISIN). ISIN in `symbol_history.csv` shows latest known value. |
