# India Equity Database — Build Plan

## Goal
Build survivorship-bias-free database of top 500 Indian listed companies (by market cap, point-in-time, quarterly rebalance) covering Mar 2018 onwards. Two CSV panels (prices, financials) + metadata + universe history with free-float market cap. Codebase split into focused scripts. Sequential execution. Rate-limited.

## History Floor

All fetchers and the universe ranker respect `config.PROJECT_START_DATE = "2018-06-30"` as the single hard floor for historical data. This date is pinned to the earliest NSE SHP XBRL filing date with reliable coverage. CLI overrides may move the effective start **later** but never earlier. The floor is enforced uniformly by `utils.dates.enforce_project_floor()`.

## Repo Layout
```
india_equity_db/
├── requirements.txt
├── config.py                  # paths, constants, sleep ranges, cache TTLs
├── universe.py                # build/refresh point-in-time top-500 universe
├── fetch_shp_xbrl.py          # fetch NSE SHP XBRL filings → shares_outstanding.csv
├── fetch_prices.py            # yfinance OHLCV + bhavcopy fallback for delisted symbols
├── fetch_financials.py        # screener.in quarterly + annual (HTML scrape, thread-pooled)
├── validate.py                # post-run sanity checks (Validator class, full-panel)
├── run_all.py                 # orchestrate full pipeline (6-step or daily mode)
├── utils/
│   ├── logger.py              # single shared run_YYYYMMDD_HHMMSS.log per process
│   ├── http.py                # canonical curl_cffi session factory + polite_sleep
│   ├── dates.py               # quarter/annual/filing date parsers + project floor guard
│   ├── cli.py                 # parse_symbol_list helper
│   ├── io.py                  # long-format CSV read/write helpers + path()
│   ├── gaps.py                # log_gap() — unified failure-log writer
│   ├── bhavcopy.py            # NSE daily bhavcopy fetcher (nselib + archive URL)
│   ├── nse_corpact.py         # NSE corporate-actions fetcher + subject parser
│   ├── price_adjust.py        # back-adjust OHLCV for splits/bonuses/dividends
│   └── lineage.py             # CLI: corporate action lineage for a symbol
├── data/
│   ├── metadata.csv
│   ├── universe_history.csv
│   ├── raw_universe.csv       # union of all ever-known symbols (never shrinks)
│   ├── symbol_history.csv     # every symbol seen in bhavcopy cache, first/last dates
│   ├── corporate_actions.csv  # audit trail: renames, demergers, mergers, delistings
│   ├── corporate_action_adjustments.csv  # split/bonus/div ledger for bhavcopy-fallback symbols
│   ├── shares_outstanding.csv
│   ├── data_gaps.csv          # unified failure log (script + symbol + reason)
│   ├── xbrl_cache/            # per-symbol XBRL files + index JSON
│   ├── nse_corpact_cache/     # per-symbol NSE corp-action API JSON (7-day TTL)
│   ├── bhavcopy_cache/        # per-day parquet snapshots ({YYYYMMDD}.parquet)
│   ├── prices_panel.csv       # long format: date, symbol, parameter, value
│   ├── financials_panel.csv   # long format: date, symbol, parameter, value (quarterly)
│   ├── financials_annual_panel.csv  # long format (annual, dates = 31-Mar-YYYY)
│   ├── validation_report.txt
│   ├── validation_outliers.csv
│   └── screener_cache/        # cached HTML per ticker ({SYMBOL}.html)
└── logs/
    └── run_YYYYMMDD_HHMMSS.log  # single file per `python script.py` invocation
```

## Dependencies (`requirements.txt`)
All versions pinned to currently-installed.
```
yfinance==1.3.0
pandas==3.0.2
numpy==2.4.4
requests==2.33.1
curl_cffi==0.15.0          # browser impersonation for screener.in and NSE
beautifulsoup4==4.14.3
lxml==6.1.0
tqdm==4.67.3
python-dateutil==2.9.0.post0
tenacity==9.1.4
nselib==2.5.1              # NSE bhavcopy API (post-2020 dates)
```
Plain `pip install -r requirements.txt`. Python 3.10+. No proxy support — direct curl_cffi only.

---

## 1. Universe Construction (`universe.py`)

### Approach — NSE-native (Point-in-Time)
Build top-500 ranked by **unadjusted close × XBRL shares**, rebalanced quarterly (Mar 31, Jun 30, Sep 30, Dec 31). Prices come from NSE bhavcopies (exchange-published, unadjusted). Shares come from NSE SHP XBRL filings — point-in-time counts that already reflect every prior split, bonus, buyback, and ESOP issuance. No corp-action adjustment is performed.

### Symbol as primary key (not ISIN)
All joins throughout the pipeline use the bare NSE symbol (e.g. `TATAMOTORS`, `TMPV`). ISIN appears as descriptive metadata only and is never a join key. Symbol renames (TATAMOTORS→TMPV) are handled by keeping each symbol's row in the data for the period it was in use — no back-stitching across renames. This model is consistent across bhavcopy, XBRL cache, shares_outstanding, universe_history, and all panels.

### Steps
1. **Seed broad universe** — superset of NSE-listed equity ever traded:
   - Current NSE equity list: `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv` (fetched via `curl_cffi` with `impersonate="chrome120"`; NSE blocks plain requests).
   - Wikipedia supplement: `https://en.wikipedia.org/wiki/List_of_companies_listed_on_the_National_Stock_Exchange_of_India` for additional historical symbols.
   - **Symbol history from bhavcopy cache** (`build_symbol_history_from_bhavcopies()`): scans all cached bhavcopy parquets and builds `data/symbol_history.csv` with `first_seen_date`, `last_seen_date`, `status`, `isin`, `source` for every symbol ever traded on NSE EQ. This ensures delisted/renamed symbols (TATAMOTORS, DHFL, RCOM, etc.) are included in `raw_universe.csv` even after they disappear from the live NSE equity list.
   - `raw_universe.csv` is union-only: never drops previously-seen symbols. Running `--refresh-universe` re-fetches live sources and re-unions; idempotent.
   - Output `data/raw_universe.csv` with columns: `symbol, isin, company_name, listing_date, delisting_date (nullable), status`.

2. **Bootstrap metadata** (`--bootstrap` flag):
   - On a fresh build, `metadata.csv` doesn't exist yet but `fetch_shp_xbrl.py` needs it. Run `universe.py --bootstrap` first.
   - Writes a flat `metadata.csv` containing every symbol from `raw_universe.csv` with no ranking columns filled.

3. **Shares outstanding** — written exclusively by `fetch_shp_xbrl.py`:
   - Source: NSE SHP XBRL filings. Point-in-time share counts; no face-value reconstruction or corp-action adjustment needed.
   - Cache to `data/shares_outstanding.csv` (columns: `symbol, date, total_shares, promoter_shares, public_shares, non_pub_nonp_shares, free_float_shares, source`).
   - Multiple filings per symbol (quarterly + material-change filings on bonus/split ex-dates).
   - History floor: `PROJECT_START_DATE` (2018-06-30).

4. **Fetch unadjusted close per quarter-end** (`utils/bhavcopy.py`):
   - For each quarter-end Q, fetch NSE bhavcopy for the nearest trading day ≤ Q. The shared `_fetch_bhavcopy_near(q, session, max_offset=7)` helper walks back up to 7 days and is used by both `_warm_bhavcopy_cache` and `compute_universe_history`.
   - Post-2020: via `nselib.capital_market.bhav_copy_with_delivery`. Pre-2020: direct archive URL `nsearchives.nseindia.com/.../cm{DD}{MMM}{YYYY}bhav.csv.zip` via `curl_cffi`.
   - Cache each day's snapshot to `data/bhavcopy_cache/{YYYYMMDD}.parquet`. ~33 quarters from 2018; near-instant on subsequent runs.
   - Filter to `SERIES == 'EQ'`. Join on bare NSE symbol (no `.NS` suffix).

5. **Compute quarterly market cap and rank** (strictly point-in-time):
   - Per symbol per quarter: use the most recent XBRL filing **on or before** quarter-end. **No forward fallback** — symbols with no filing on/before Q are excluded from that quarter's ranking. (Earlier versions back-filled within +90 days post-quarter, which leaked future shares data into the backtest universe; removed in v0.4.0.)
   - `mcap(symbol, Q) = unadjusted_close(Q) × total_shares(most_recent_filing ≤ Q)`.
   - Drop symbols with missing close or no qualifying XBRL filing.
   - Store `data/universe_history.csv`:
     ```
     quarter_end_date, rank, symbol, isin,
     market_cap_inr, free_float_market_cap_inr,
     close_inr, shares_outstanding, free_float_shares
     ```
   - Ranking is by full `market_cap_inr`; `free_float_market_cap_inr` is stored for downstream use.

6. **Master union ticker list**:
   - After ranking, `metadata.csv` is rewritten to all symbols that appeared in the top 500 in ANY quarter (left-join from universe symbols, so delisted tickers not in the current NSE equity list are preserved with NaN for NSE-sourced fields).
   - Columns: `symbol, isin, company_name, sector, industry, first_in_universe_date, last_in_universe_date, status`.
   - `first_in_universe_date` and `last_in_universe_date` are anchored to ≥ `PROJECT_START_DATE`.
   - Column padding + reorder is handled by the shared `_finalize_metadata` helper (used by both `build_metadata` and `_write_bootstrap_metadata`).
   - After saving metadata, `financials_panel.csv` and `financials_annual_panel.csv` are pruned to remove any rows for symbols not in the universe (guards against stale bootstrap-era data).

### Notes
- Ticker key: **bare NSE symbol** (e.g. `RELIANCE`). No `.NS` suffix anywhere in the universe pipeline.
- `prices_panel.csv` (from `fetch_prices.py`) uses yfinance `auto_adjust=True` and is a **separate concern** — it is the correct source for total-return computations but must not be used for market cap ranking.
- Bhavcopy cache is permanent — delete individual parquets only if you suspect corrupt data.

---

## 2. SHP XBRL Fetcher (`fetch_shp_xbrl.py`)

### Approach — NSE API + XBRL parsing

Fetches per-symbol Shareholding Pattern XBRL filings from NSE. Each filing contains point-in-time share counts for total, promoter, public, and non-promoter-non-public shareholders.

### Input
`metadata.csv` → list of symbols. `PROJECT_START_DATE` as default floor.

### Logic per symbol (executed in a thread pool, `--workers N` default from `config.XBRL_WORKERS` = 8)
Each worker thread holds its own `curl_cffi` session in `threading.local` storage. On first use, the thread builds the session and runs the NSE warmup once; all subsequent symbols handled by that thread reuse the same session and cookies. Total warmup cost for a 500-symbol run is `workers` warmups, not `500`.

1. **Warm up NSE session** (once per worker thread, lazily on first symbol):
   - `GET https://www.nseindia.com/`
   - `GET https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern`
   - Sets Akamai cookies (`bm_sv`, `_abck`, `nsit`) that the JSON API endpoints require.

2. **Fetch filings index** from `https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol={SYM}`. Cache to `data/xbrl_cache/{SYMBOL}/_index.json` with 1-day TTL.

3. **Per filing**: skip if `xbrl` field ends in `"-"` or `"null"` (placeholder). Skip if `filing_date < PROJECT_START_DATE`.

4. **Download XBRL XML** to `data/xbrl_cache/{SYMBOL}/{YYYY-MM-DD}_{recordId}.xml`. Cached permanently; re-downloaded only with `--force`.

5. **Parse** using regex on `NumberOfShares` elements with context names:

   | Logical field | Context name pattern |
   |---|---|
   | `total_shares` | `ShareholdingPattern(I\|_ContextI)` |
   | `promoter_shares` | `ShareholdingOfPromoterAndPromoterGroup(I\|_ContextI)` |
   | `public_shares` | `PublicShareholding(I\|_ContextI)` |
   | `non_promoter_non_public` | `SharesHeldByNonPromoterNonPublicShareholders(I\|_ContextI)` |

6. **Free-float**: `free_float_shares = total_shares − promoter_shares`. Raw `public_shares` and `non_pub_nonp_shares` are also persisted so the validator can run an additive integrity check (`total ≈ promoter + public + non_pub_nonp`) rather than a tautological one.

7. **Dedup** on `(symbol, date)` keeping the highest positive integer `recordId` (handles revised filings; non-numeric or negative ids are coerced to 0 so they lose dedup priority).

### Output
`data/shares_outstanding.csv` — columns: `symbol, date, total_shares, promoter_shares, public_shares, non_pub_nonp_shares, free_float_shares, source`.

### XBRL schema versions
Two naming conventions observed; both handled by the regex:
- 2018-03-31 schema: context names like `ShareholdingPatternI`
- 2022-09-30 schema: context names like `ShareholdingPatternI` (same)
- 2025-10-31 schema: context names like `ShareholdingPattern_ContextI`

---

## 3. Price Fetcher (`fetch_prices.py`)

### Input
`metadata.csv` → list of unique symbols.

### Logic
1. Default fetch start = `config.PROJECT_START_DATE`. `--start-date YYYY-MM-DD` overrides (must be ≥ `PROJECT_START_DATE`; enforced via `utils.dates.enforce_project_floor`).
2. Per-symbol effective start = `max(first_in_universe_date, default_start)`.
3. Single batch call: `yf.download([f"{s}.NS" for s in symbols], start=..., end=today, auto_adjust=True, actions=False, group_by="ticker", threads=True)`.
4. Per symbol, the `_slice_yf_for_symbol(prices_raw, sym)` helper slices the MultiIndex, keeps `open, high, low, close, volume`, melts to long. Symbols returning `None` (missing ticker / all-NaN) are routed to the bhavcopy fallback. Final panel is concatenated and written via `write_long_panel`. No merge with prior runs — each invocation rebuilds the panel from scratch.

### Storage Format — `prices_panel.csv`
Long format: `date, symbol, parameter, value`.
- `parameter` values: `open`, `high`, `low`, `close`, `volume`.
- `date` = trading date (daily, UTC-normalized, no timezone).
- File is overwritten each run (no cross-run merge / dedup).

---

## 4. Financials Fetcher (`fetch_financials.py`)

### Approach — HTML scraping (no login required)
The screener.in XLSX export requires authentication. Instead, we parse financial tables directly from the public HTML company page (`/company/{SYMBOL}/consolidated/`), which contains all quarterly and annual data.

Screener is the source of truth for **fundamentals only** (P&L, BS, CF). Shares outstanding are sourced from XBRL (see section 2) and are not touched by this module.

### Input
`metadata.csv` → symbols (NSE).

### Logic per symbol (executed in a thread pool, `--workers N` default from `config.FINANCIALS_WORKERS` = 8)
The worker is a pure function `_worker(symbol, force, project_start_ts) -> {quarterly?, annual?, failure?}`; all CSV writes happen on the main thread after `as_completed`, so there are no write races. The `curl_cffi` session is created once per thread (via `threading.local`) and reused across all symbols/retries that thread handles — screener doesn't need a cookie warmup, but session reuse avoids paying the `chrome120` impersonation setup cost per call.

1. **Fetch & cache HTML** via `curl_cffi` with `impersonate="chrome120"`. Cached to `data/screener_cache/{symbol}.html` (path from `config.SCREENER_CACHE_DIR`). Re-downloaded if older than `SCREENER_CACHE_DAYS` (7 days) or `--force` passed. The `_has_tables(html)` validator is run on cache hits to detect dead pages (sections present but no date columns).

2. **Parse financial tables from HTML**:
   - Quarterly section (`id="quarters"`): quarterly P&L. Date headers like "Mar 2024" → `2024-03-31` via `utils.dates.parse_quarter_date`.
   - Annual sections (`id="profit-loss"`, `id="balance-sheet"`, `id="cash-flow"`): annual data. All dates forced to `31-Mar-YYYY` via `utils.dates.parse_annual_date`.
   - Line item names normalized to `snake_case`.

3. **Filter quarterly data to `date >= PROJECT_START_DATE`** before appending to the panel. If quarterly data exists but all dates are before the cutoff, log `NoRecentData` to `data_gaps.csv` rather than silently discarding.

4. **Log failures to `data/data_gaps.csv`** (via `utils.gaps.log_gap("fetch_financials", ...)`) in all modes:
   - `PageFetchFailed`: screener page could not be downloaded after all retries.
   - `ParseFailed`: HTML downloaded but no financial tables found.
   - `NoRecentData`: quarterly section parsed but all dates fall before `PROJECT_START_DATE`.
   - `NoQuarterlyData`: page downloaded and annual data exists, but no quarterly P&L section.
   - `WorkerException`: any unhandled exception in the thread worker.

5. **Melt to long format and merge into panels** (`financials_panel.csv`, `financials_annual_panel.csv`).

### Derived parameters
- `ebit = ebitda - depreciation` when both present.
- `tax_implied = pbt - net_profit` (approximation; exact only when there is no minority interest, share of associates, or discontinued operations).
- Annual: `net_equity = share_capital + reserves`; `total_liabilities = total_assets - net_equity` (non-equity liabilities); `fcf = cfo + cfi` fallback if screener doesn't provide free_cash_flow directly.

### Date Conventions
- **Quarterly dates**: last calendar day of the quarter-end month shown by screener.
- **Annual dates**: always `31-Mar-YYYY`, reflecting India's April–March fiscal year.

### Storage Format
Both panels use the same long format: `date, symbol, parameter, value`.
- `financials_panel.csv` — quarterly P&L; `date` = quarter-end.
- `financials_annual_panel.csv` — annual BS + CF; `date` = `31-Mar-YYYY`.

### Banking / NBFC companies

| Parameter | Non-financial | Banking / NBFC |
|---|---|---|
| `total_revenue` | Sales | Revenue |
| `ebitda` | Sales - Expenses | **Absent** |
| `ebit` | ebitda - depreciation | **Absent** |
| `financing_profit` | Absent | NII (Net Interest Income) |
| `financing_margin` | Absent | NIM % |
| `gross_npa`, `net_npa` | Absent | NPA quality ratios (quarterly) |
| `deposits` | Absent | Core funding metric (annual BS) |
| `total_debt` | From "Borrowings" | From "Borrowing" (singular on screener) |

---

## 5. Storage Schemas (Summary)

### `metadata.csv`
| symbol | isin | company_name | sector | industry | first_in_universe_date | last_in_universe_date | status |

### `universe_history.csv`
| quarter_end_date | rank | symbol | isin | market_cap_inr | free_float_market_cap_inr | close_inr | shares_outstanding | free_float_shares |

All `quarter_end_date` values are ≥ `PROJECT_START_DATE`.

### `shares_outstanding.csv`
| symbol | date | total_shares | promoter_shares | public_shares | non_pub_nonp_shares | free_float_shares | source |

`source` = `"nse_shp_xbrl"`. Multiple rows per symbol (one per XBRL filing). `free_float_shares = total_shares - promoter_shares` always holds exactly. `public_shares` and `non_pub_nonp_shares` are the raw XBRL fields, enabling an additive integrity check at validation time.

### `data_gaps.csv`
| script | symbol | reason | error_msg | recorded_date |

Unified failure log. Every fetcher appends here via `utils.gaps.log_gap()`. `script` ∈ {`fetch_financials`, `fetch_shp_xbrl`, `fetch_prices`}. Reason codes are namespaced per script.

### `prices_panel.csv`
Long format. Columns: `date, symbol, parameter, value`.
- `parameter` ∈ {`open`, `high`, `low`, `close`, `volume`}
- `date`: daily trading dates (ISO format, no timezone)
- Deduplicated on `(date, symbol, parameter)`

### `financials_panel.csv`
Long format. Columns: `date, symbol, parameter, value`.
- `date`: quarter-end dates (Mar 31 / Jun 30 / Sep 30 / Dec 31), ≥ `PROJECT_START_DATE`
- Common parameters: `total_revenue`, `operating_expenses`, `ebitda`, `ebit`, `depreciation`, `interest_expense`, `pbt`, `tax_implied`, `net_profit`, `eps`

### `financials_annual_panel.csv`
Long format. Same columns.
- `date`: always `31-Mar-YYYY` (Indian fiscal year end), ≥ `PROJECT_START_DATE`
- Additional parameters: BS items (`total_assets`, `net_equity`, `total_liabilities`, `total_debt`, `fixed_assets`, `cwip`, `investments`, `other_assets`, `other_liabilities`) and CF items (`cfo`, `cfi`, `cff`, `net_cash_flow`, `fcf`). Banks additionally include `deposits`.

---

## 6. HTTP Module (`utils/http.py`)

```python
def make_session(warmup: bool = True) -> cffi_requests.Session:
    # curl_cffi chrome120 impersonation; optional nseindia.com warmup
def polite_sleep(min_s, max_s): time.sleep(random.uniform(min_s, max_s))
```

`fetch_shp_xbrl.py` and `fetch_financials.py` each maintain a `threading.local` session cache so that a worker thread builds its `curl_cffi` session (and, for XBRL, runs the NSE warmup) exactly once across all the symbols it handles. `make_session(warmup=False)` is the caller pattern; the fetcher's own `_warmup(session)` is invoked alongside session creation for XBRL only — screener has no cookie warmup. Note that `polite_sleep` is per-thread and therefore does *not* throttle the aggregate request rate to NSE; peak concurrency is bounded by `XBRL_WORKERS` / `FINANCIALS_WORKERS`. Stay within ~8–16 to avoid Akamai throttling.

---

## 7. Execution Order

`universe.py` needs `shares_outstanding.csv` to rank, but `fetch_shp_xbrl.py` needs `metadata.csv` from `universe.py`. The `--bootstrap` flag breaks this circular dependency.

```bash
# One-time / quarterly — required order
python universe.py --bootstrap --refresh-universe   # writes flat metadata.csv (all symbols, no ranking)
python fetch_shp_xbrl.py                           # writes shares_outstanding.csv from NSE XBRL
python universe.py                                  # ranks using bhavcopy × XBRL shares; rewrites metadata.csv to universe symbols
python fetch_financials.py                          # writes financials panels; reads metadata (universe symbols only)
python fetch_prices.py                              # adjusted OHLCV for total-return series
python validate.py

# Routine daily
python fetch_shp_xbrl.py     # pick up any new quarterly or material-change filings
python fetch_prices.py
python validate.py
```

### run_all.py modes

| Mode | Steps | Notes |
|---|---|---|
| `smoke` | All 6 | 3 symbols (RELIANCE, TCS, ZYDUSWELL) |
| `full` | All 6 | All metadata symbols |
| `quarterly` | All 6 | Alias for `full` |
| `force` | All 6 | `--force` passed to `fetch_shp_xbrl` and `fetch_financials` |
| `daily` | 3 (xbrl + prices + validate) | Incremental update only |

---

## 8. Validation (`validate.py`)

Implemented as a `Validator` class — each `check_*(v: Validator)` function records results via `v.check(name, cond, warn_cond, detail)` or `v.record(name, status, detail)`. No global state. Most checks run on the full panel (no sampling).

### Shares Outstanding Checks
- Additive integrity: `total_shares ≈ promoter + public + non_pub_nonp` (within 1 share). FAIL if more than 5 violations. (The prior `promoter + free_float == total` check was tautological — `free_float` is derived as `total - promoter` upstream — and was replaced in v0.4.0.)
- Bounds: `free_float_shares > 0`, `free_float_shares ≤ total_shares`, `promoter_shares ≥ 0` → FAIL if any violation.
- Metadata symbols with 0 XBRL filings → WARN.

### Universe Checks
- `universe_history.csv` has ≥ 28 distinct `quarter_end_date` values (FAIL), ≥ 20 (WARN).
- Each quarter has 490–510 ranked symbols (FAIL if < 490).
- No duplicate `(quarter_end_date, symbol)` pairs.
- `metadata.csv` symbol count ≥ 500 and ≤ 1500.
- Every symbol in `universe_history` exists in `metadata.csv`.
- All `quarter_end_date` values ≥ `PROJECT_START_DATE`.

### Price Panel Checks (long format, full panel)
- All metadata symbols present in `prices_panel.csv`.
- Each symbol has exactly 5 parameters (open/high/low/close/volume).
- No duplicate `(date, symbol, parameter)` rows.
- ≥ 80% non-null close values in active window per symbol (vectorized full-panel merge with metadata's first/last universe dates).
- No negative prices or volume.
- OHLC sanity (full-panel pivot): high ≥ low, high ≥ open, high ≥ close, low ≤ open, low ≤ close.
- Day-over-day close returns > 50% logged to `validation_outliers.csv` (full-panel `groupby.pct_change()`). WARN, not FAIL — may reflect genuine events.
- Last date within 5 trading days of today.
- `prices_fallback_coverage`: % of `xbrl_unavailable_for_delisted` symbols recovered via bhavcopy fallback (reads `data_gaps.csv`).
- `corp_action_adjustment_symbol_coverage`: every symbol in `corporate_action_adjustments.csv` has rows in `prices_panel.csv`.

### Financials Panel Checks (long format, full panel)
- Each metadata symbol has ≥ 1 row OR appears in `data_gaps.csv` with `script == "fetch_financials"` → FAIL if any symbol is absent from both (indicates a silent pipeline drop).
- Date months ∈ {3, 6, 9, 12} (quarter-end alignment).
- Core parameters present per symbol (full panel set-based check): `total_revenue`, `net_profit`, `eps` plus either `ebitda` (non-financials) or `financing_profit` (banks/NBFCs).
- No future-dated rows.
- ≥ 40 quarters for ≥ 80% of long-tenure symbols.

### Cross-Panel Checks
- All symbols in price/financial panels exist in `metadata.csv`. Since `universe.py` prunes financial panels to metadata symbols after each ranking run, orphan symbols from prior bootstrap scrapes will not be present.
- Universe symbols missing from BOTH panels → FAIL.

### Failed-Log Checks
- Unique symbols in `data_gaps.csv` with `script == "fetch_financials"` < 15% of metadata count.

---

## 9. Logging
- All scripts use `utils/logger.py` → `logs/run_YYYYMMDD_HHMMSS.log` + stdout. **One file per process**: every `get_logger(name)` call within one `python script.py` invocation attaches the same shared `FileHandler`, so a `run_all.py` invocation produces one log file rather than 6+.
- Stdout stream configured with `errors="replace"` to handle Windows cp1252 terminals.
- Log levels: INFO (per-symbol progress), WARN (retries, parse anomalies), ERROR (final failures), DEBUG (sleep/cache details).

---

## 10. Config Defaults (`config.py`)
Organized into sections: Paths, Universe (incl. `PROJECT_START_DATE`), HTTP politeness, Parallelism, Caches.
```python
# Universe
PROJECT_START_DATE = "2018-06-30"   # hard floor for all history
UNIVERSE_SIZE = 500

# HTTP politeness (per-thread sleeps; do not throttle aggregate rate)
MIN_SLEEP_NSE,      MAX_SLEEP_NSE      = 1.0, 2.0   # NSE bhavcopy + XBRL
MIN_SLEEP_SCREENER, MAX_SLEEP_SCREENER = 2.0, 4.0
MAX_RETRIES = 3

# Parallelism (peak concurrent requests = workers; stay 8-16 for NSE/Akamai)
XBRL_WORKERS = 8
FINANCIALS_WORKERS = 8

# Caches
SCREENER_CACHE_DIR    = os.path.join(DATA_DIR, "screener_cache")
SCREENER_CACHE_DAYS   = 7
XBRL_CACHE_DIR        = os.path.join(DATA_DIR, "xbrl_cache")
XBRL_INDEX_CACHE_DAYS = 1
BHAVCOPY_CACHE_DIR    = os.path.join(DATA_DIR, "bhavcopy_cache")
NSE_CORPACT_CACHE_DIR = os.path.join(DATA_DIR, "nse_corpact_cache")
NSE_CORPACT_CACHE_DAYS = 7
```
No proxy config — direct curl_cffi only. Worker defaults can be overridden ad-hoc with `--workers N` on the individual scripts.

---

## 11. Known Limitations
- Screener HTML only shows the last 13 quarters; `financials_quarter_coverage_40q` will warn until 10+ years of re-scraping accumulates.
- Screener XLSX export requires authentication — not used. HTML parsing covers the same data for public pages.
- yfinance >= 0.2.40 requires its own curl_cffi session; do NOT pass `requests.Session` to `yf.Ticker()`.
- XBRL coverage starts Mar 2018; pre-2018 universe history is not supported.
- NSE SHP XBRL filings are quarterly plus occasional material-change filings (e.g. bonus/split ex-dates). Mid-year corporate actions that do not trigger a filing will not appear until the next quarter-end filing.
- NSE bhavcopy `nselib` route covers dates ≥ 2020-01-01 only. Earlier dates use the `nsearchives.nseindia.com` archive URL. Both are cached as parquet.
- Truly delisted symbols: bhavcopy has their pre-delisting data (they appear in `symbol_history.csv` and `raw_universe.csv`), but if NSE's XBRL API returns no filings under the old symbol, they will be absent from `universe_history.csv`. These cases are recorded in `data/data_gaps.csv` with `script=fetch_shp_xbrl` and `reason=xbrl_unavailable_for_delisted`. Future work: scrape BSE or SEBI filings for these symbols.
- No return splicing across renames/mergers: `fetch_prices.py` is unchanged. Analysts wanting a continuous TATAMOTORS→TMPV price series must join via `data/corporate_actions.csv` manually. Use `python -m utils.lineage --symbol TATAMOTORS` to find the link.
- Free-float reconciliation post-demerger: TMPV + TMCV free-float sum ≠ pre-demerger TATAMOTORS free-float. This is correct; do not reconcile.
- Symbol reincarnation (delisted ticker reused by an unrelated company): `symbol_history.csv` collapses both into a single `(first_seen, last_seen)` pair. Validator unexplained-entry checks surface such edge cases for manual review.
- Current Assets and Current Liabilities are not available from screener's public HTML.

## 12. Out of Scope
- Intraday data
- Standalone (non-consolidated) financials
- Derivative/F&O data
- Analysis layer (raw storage only)
- Official Nifty 500 membership reconstruction (requires NSE press release scraping)
- Pre-2018 universe history from any alternate shares source
