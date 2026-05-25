# Pipeline changelog

Backtest module changelog (v0.5.0+) lives in [../../backtest/docs/changelog.md](../../backtest/docs/changelog.md).
The v0.6.0 refactor that split pipeline and backtest into separate modules is
recorded in the root [../../CHANGELOG.md](../../CHANGELOG.md).

## v0.4.4 — 2026-05-25

Bhavcopy fallback inverted from symbol-major to date-major scan.

### Why
Previous `_fallback_one` called `fetch_daily_range(symbol, …)` per fallback
symbol, which looped every trading day in the symbol's window and filtered one
bhavcopy for one row. Bhavcopy parquets are cached, so network cost was fine,
but each subsequent fallback symbol still paid `N_days` parquet reads + filters
to extract a single row per day from a panel that already held every EQ symbol.
Cost scaled as `N_fallback_symbols × N_days` parquet reads for no reason.

### Changed
- **`fetch_prices.py::_build_fallback_panels(symbols, default_start, today, session)`**
  — new helper. Computes per-symbol `[start, end]` windows once, takes the
  union range, loops days once, loads each daily bhavcopy via
  `fetch_bhavcopy(d)`, filters to the fallback-symbol set, concats chunks,
  then `groupby("symbol")` to produce `dict[symbol → wide OHLCV df]` honoring
  each symbol's own window. Parquet reads drop from
  `N_symbols × N_days` to `N_days`.
- **`fetch_prices.py::_adjust_fallback_panel(symbol, raw_df, …)`** — replaces
  `_fallback_one`. Takes a pre-built panel, fetches corp actions, applies
  adjustments, returns long-format. Corp-action fetch (`get_adjustments`)
  remains per-symbol — unavoidable, different endpoint.
- Main fallback block: builds panels once, then iterates fallback symbols only
  for corp-action fetch + adjustment. `bhavcopy_no_rows_in_range` gap log
  moved out of the (now removed) per-symbol fetch helper and into the main
  loop.

### Operational notes
- No schema changes. Output identical to v0.4.3 for the same inputs.
- Re-run not required; this is a performance refactor, not a correctness fix.

## v0.4.3 — 2026-05-25

Financials lookback buffer for YoY / multi-quarter signal warm-up.

### Added
- **`config.FINANCIALS_HISTORY_LOOKBACK_QUARTERS`** (default `6`). Extends
  the `financials_panel.csv` and `financials_annual_panel.csv` retention
  window backward by this many quarter-ends before `PROJECT_START_DATE`.
  Universe and XBRL remain floored at `PROJECT_START_DATE` — financials
  joins prices as a signal-warmup exception to the global floor.
- `utils.dates.financials_history_floor()` — returns the extended floor as a
  `pd.Timestamp` (= `PROJECT_START_DATE − N quarters`, computed via
  `pd.DateOffset(months=N*3)`).

### Why
`earnings_growth_yoy` and similar fundamental signals compute YoY changes
against a prior-year filing. With financials previously floored at
`PROJECT_START_DATE = 2018-06-30`, the "prior" row didn't exist until
roughly mid-2019 (depending on the `reporting_lag_days` setting in the
backtest config), so the YoY signal was effectively absent for the first
~14 months of backtest. Retaining 6 quarters of pre-floor history gives
day-one prior-year readings with a 1-quarter buffer for missed filings.

### Changed
- `fetch_financials.py::_save_panel` and `_worker` now filter against
  `financials_history_floor()` instead of `PROJECT_START_DATE`. The
  `NoRecentData` data-gap message reflects the new floor.

### Operational notes
- **Re-fetch required**: existing `financials_panel.csv` only goes back to
  `PROJECT_START_DATE`. Cached screener HTML already contains the full
  history — re-run `python -m pipeline.fetch_financials --force` to re-parse
  and pick up the pre-floor quarters.
- No schema changes; just additional rows in both financials panels.

## v0.4.2 — 2026-05-25

Prices-only lookback buffer for backtest signal warm-up.

### Added
- **`config.PRICE_HISTORY_LOOKBACK_DAYS`** (default `365`). Extends the
  `prices_panel.csv` fetch window backward by this many days before
  `PROJECT_START_DATE` (and before each symbol's `first_in_universe_date`).
  Universe, XBRL, and financials remain floored at `PROJECT_START_DATE` —
  this is a **prices-only** exception.
- `utils.dates.price_history_floor()` and `enforce_price_history_floor()` —
  the prices-side analogues of `enforce_project_floor`. Lower bound for any
  `fetch_prices.py --start-date` override.

### Why
Backtest signals with trailing lookbacks (e.g. `momentum_12_1` reads the
12-month-trailing close) need ~252 trading days of price history *before*
their first scoring date. Without a buffer, the first rebal date had no
prior closes and signal-required weighting modes (`ffmcap_tilt`,
`score_weighted`) raised at run time. Pulling history one year prior to
PROJECT_START_DATE gives every signal a warm-up runway at the very first
rebal date, regardless of rebal frequency.

### Changed
- `fetch_prices.py` defaults: yfinance batch and bhavcopy fallback both start
  at `PROJECT_START_DATE − PRICE_HISTORY_LOOKBACK_DAYS`. Per-symbol start in
  `_symbol_date_range` shifts to `first_seen_date − lookback` (still floored
  at the absolute lookback floor), so late universe joiners also get
  pre-entry history.
- `fetch_prices.py --start-date` now validates against `price_history_floor()`
  (the extended floor), not `PROJECT_START_DATE`. Error message updated.

### Operational notes
- **Re-fetch required**: existing `prices_panel.csv` only goes back to
  `PROJECT_START_DATE`. Re-run `python -m pipeline.fetch_prices` to pick up
  the lookback rows. Backtest wide-pivot caches (`backtest/data/cache/*.parquet`)
  invalidate on source mtime automatically — no manual cache clearing.
- No schema changes; just additional rows.

## v0.4.1 — 2026-05-25

Follow-up to v0.4.0: pipeline ergonomics + session reuse. No data/schema changes.

### Added
- `config.XBRL_WORKERS` and `config.FINANCIALS_WORKERS` (both default 8) — single source of truth for fetcher parallelism. Per-script `--workers N` still overrides ad-hoc.
- `SCREENER_CACHE_DIR` config var. Cached screener HTML now lives at `data/screener_cache/` (matches `bhavcopy_cache/`, `xbrl_cache/`, `nse_corpact_cache/` naming pattern). Was `data/raw_screener/`.
- Per-thread `curl_cffi` session reuse in `fetch_shp_xbrl.py` and `fetch_financials.py` via `threading.local`. XBRL warmup (NSE Akamai cookie dance) now runs once per worker thread instead of once per symbol — for an 8-worker 500-symbol run that's 8 warmups instead of 500, saving ~minutes of pure sleep-tax. Screener doesn't need warmup but still benefits from skipping `chrome120` impersonation setup per request.

### Changed
- `config.py` reorganized into labeled sections: Paths, Universe, HTTP politeness, Parallelism, Caches. No value changes from this reorg.
- `MIN_SLEEP_NSE, MAX_SLEEP_NSE` tuned `0.4, 1.0` → `1.0, 2.0` based on observed NSE throttling at higher worker counts.
- `run_all.py --workers N` flag **removed**. Worker counts now come from config (see Added). The flag conflated two independently-tuned settings; users on this project rarely needed the one-shot override.
- `fetch_financials.py::fetch_company_page._fetch_url` — session is now `_thread_session()` (cached) instead of `make_session(warmup=False)` per retry attempt.
- `fetch_shp_xbrl.py::_worker` — no longer creates a session or runs warmup; both move to `_thread_session()` which lazy-inits per thread.

### Notes
- **NSE rate limits:** per-thread `polite_sleep` does *not* throttle aggregate request rate to NSE. Peak concurrency is bounded by `XBRL_WORKERS`. Empirical safe band is 8–16 workers; pushing to 32+ triggers Akamai 403/429 regardless of sleep values. Docs in README and plan.md now spell this out.
- No data migration: `data/raw_screener/` did not exist on disk in this checkout. If you have an existing cache from v0.4.0, rename the folder to `screener_cache/` or let the next run re-download.

## v0.4.0 — 2026-05-25

Cross-cutting refactor sweep. Six phases: proxy strip, helper centralization, bug fixes, DRY, perf, consistency. No behavior change to the panels themselves; all output schemas are unchanged except where flagged below.

### Removed
- **All proxy support.** `config.PROXY_MODE`, `config.PROXIES`, `FREE_PROXY_*`, `ProxyPool`, `fetch_free_proxies`, `make_retry_decorator` deleted. Direct `curl_cffi` only. README + plan.md updated.
- `utils/rate_limit.py` deleted (gutted to nothing after proxy strip; `polite_sleep` moved into `utils/http.py`).
- `fetch_financials.extract_company_id` and the `metadata.screener_company_id` column. Dead path: the id was written but never read to drive an API call. Existing `metadata.csv` migrated in place to drop the column.
- `fetch_financials.adj_net_profit` parameter — was literally `net_profit` copied (no exceptional-item data available from screener HTML).
- Sample-of-N validation (50 symbols for close-coverage, 20 for OHLC sanity / return outliers / financials core items). Replaced by full-panel vectorized checks. The new outlier check found 32 moves >50% (vs ~0 from the sample) and surfaced 4 real OHLC violations the sample missed.
- `data/failed_financials.csv` and `data/known_data_gaps.csv` files. Merged into a single `data/data_gaps.csv` (1593 historical rows migrated).
- 4 duplicate `_make_session()` / `_cffi_session()` factories across fetchers — replaced by one `utils.http.make_session(warmup=True)`.
- Local date parsers in 3 fetchers — replaced by `utils.dates.parse_filing_date`, `parse_quarter_date`, `parse_annual_date`, `month_end`.
- 4 hand-rolled `args.symbols.split(",")` blocks — replaced by `utils.cli.parse_symbol_list`.
- 3 PROJECT_START guard blocks — replaced by `utils.dates.enforce_project_floor`.

### Added
- `utils/http.py` — canonical `make_session(warmup=True)` + `polite_sleep`.
- `utils/dates.py` — `parse_filing_date`, `parse_quarter_date`, `parse_annual_date`, `month_end`, `enforce_project_floor`, `quarter_end_dates`.
- `utils/cli.py` — `parse_symbol_list(args.symbols)`.
- `utils/gaps.py` — `log_gap(script, symbol, reason, error_msg)` writes to unified `data/data_gaps.csv` with columns `{script, symbol, reason, error_msg, recorded_date}`.
- `fetch_financials.py` — thread-pooled with `--workers N` arg (default 4). Each worker is a pure function returning `{quarterly, annual, failure}`; the main thread handles all CSV writes (no write races). Wired through `run_all.py`. Expected ~4× speedup on a full 500-symbol run.
- `validate.py::Validator` class — replaces the module-level `results: list`. Each `check_*` function takes a `Validator` arg.
- `fetch_shp_xbrl.py` — `shares_outstanding.csv` now persists `public_shares` and `non_pub_nonp_shares` alongside the derived `free_float_shares`, so the integrity check can finally run additively rather than tautologically.
- `universe.py::_fetch_bhavcopy_near(q, session, max_offset=7)` — single helper used by both `_warm_bhavcopy_cache` and `compute_universe_history`.
- `universe.py::_finalize_metadata(meta, label)` — shared column-padding + write helper used by both `build_metadata` and `_write_bootstrap_metadata`.
- `fetch_prices.py::_slice_yf_for_symbol(prices_raw, sym)` — extracts the yf-to-long conversion into one pure function; main loop is now 5 lines.

### Fixed
- **Point-in-time look-ahead bias.** `universe.py::compute_universe_history` previously back-filled missing XBRL filings with the *next* filing within +90 days post-quarter-end. This leaked future shares data into the ranking. Removed: ranking is now strictly point-in-time (filing date ≤ quarter end). Symbols with no filing on/before the quarter are now excluded from that quarter.
- **Tautological shares integrity check.** `validate.py::shares_integrity_sum` previously checked `promoter + free_float == total`, which is true by construction since `free_float = total - promoter` upstream. Replaced with `total ≈ promoter + public + non_pub_nonp` (sourced from raw XBRL fields).
- **Negative recordId mishandling.** `fetch_shp_xbrl.py` dedup at `(symbol, date)` sorted by recordId previously did `str(rid).lstrip("-").isdigit()`, which converted negatives to negative ints — flipping dedup priority. Now requires positive integer.
- **Schema-mismatch silent overwrite.** `fetch_shp_xbrl.py` previously replaced an old-schema `shares_outstanding.csv` without backing it up. Now writes `.bak` first and logs loudly.
- **Stock-split parser too narrow.** `utils/nse_corpact.py` only matched "from Rs.X to Rs.Y". 6 real splits in the cache (CORPBANK, DFMFOODS, HDFCBANK, JMCPROJECT, TATACOFFEE, TV18BRDCST) were silently dropped because their subject strings were "From Rs 10/- Per Share To Rs 2/- Per Share" or "Fv Split Rs.10/- To Rs.2/". Patterns broadened to cover from/to with arbitrary text between numbers, "FV of Rs.X of Rs.Y", and pure ratio `split N:M` / `sub-division N:M`. Re-audit of cache: 0 unparsed split-keyword subjects.
- **Silently-dropped large dividends.** `utils/price_adjust.py` skipped dividend events where `amount >= close_prev`. Special / liquidation dividends are real (e.g. 100% one-time payouts). Now clamps `price_factor` to 0.01 and logs a WARN so the event still affects the back-adjustment chain.
- **Misleading parameter name.** `fetch_financials.py::tax` renamed to `tax_implied` — it's an approximation (`pbt - net_profit`) and is exact only when there is no minority interest, share of associates, or discontinued operations.
- **Validation references a never-written file.** `validate.py::check_failed_logs` checked `failed_prices.csv` which was never produced anywhere. Removed; `data_gaps.csv` covers price failures via `script == "fetch_prices"`.

### Changed
- **Single log file per process.** `utils/logger.py` now creates one shared `FileHandler` per `python script.py` invocation. Previously, every `get_logger(name)` call created a new `run_{ts}.log`, so one `run_all.py` produced 6+ log files.
- **`--workers` arg added to `fetch_financials.py`** and wired through `run_all.py`.
- **Hardcoded NSE EQUITY_L schema.** `universe.py::fetch_nse_equity_list` previously did fuzzy column-name lookups (`next(c for c in df.columns if "symbol" in c)`). Now requires explicit columns and raises `KeyError` loudly if NSE renames them — fail loud, not silent.
- **Vectorized isin merge.** `universe.py::build_raw_universe` now uses `combined["symbol"].map(isin_live).fillna(combined["isin"])` instead of `apply(axis=1)` lambdas. ~1000× faster on 5k+ symbols.
- **Retry waits widened.** `fetch_financials` retry `wait_exponential` bumped from `(2, 32)s` to `(30, 90)s` — better matches NSE/screener 429 cooldowns.
- **`fetch_prices.py --start` renamed to `--start-date`** for consistency with `universe.py` and `fetch_shp_xbrl.py`.
- **All `sys.exit(N)` replaced with `raise SystemExit(N)`.**
- **`requirements.txt`** — pinned all versions to currently-installed (`yfinance==1.3.0`, `pandas==3.0.2`, `numpy==2.4.4`, etc.). Dropped `openpyxl` (unused). Added `nselib==2.5.1` (was missing but used by `utils/bhavcopy.py`).
- **CLI help strings** now read `PROJECT_START_DATE` from config dynamically — no more stale `>= 2018-03-31` text when the actual value is `2018-06-30`.

### Notes
- Pathlib unification was considered (some files use `pathlib`, others `os.path`) but deferred. Each file is internally consistent; cross-file churn would be cosmetic without functional payoff.
- Test suite still absent (deferred to a later milestone).

## v0.3.1 — 2026-05-24

### Fixed
- `utils/bhavcopy.py::fetch_bhavcopy` — falls back to archive URL when nselib raises non-FileNotFoundError exceptions. nselib's `bhav_copy_with_delivery` has data-quality issues for specific dates (e.g. 2022-08-08 raises `UnicodeDecodeError: 'utf-8' codec can't decode byte 0x90`); the old-style NSE archive URL still serves these files cleanly. Recovers ~1 trading day per affected symbol.
- `validate.py::check_survivorship_bias` exits check — previously flagged any symbol that dropped out of the top-500 ranking, which conflated normal mcap-rank churn with genuine survivorship gaps. Now requires `symbol_history.status == 'delisted_or_renamed'` AND `last_seen_date` within 12 months of universe exit. False-positive count dropped from 381 → 0 after seeding `corporate_actions.csv`.
- `validate.py::check_survivorship_bias` entries check — demoted from FAIL to WARN. We cannot reliably distinguish "company existed pre-project-start but only grew into top 500 later" (normal ranking growth) from "true survivorship gap where a tradeable top-500 company was missed at project start" without historical mcap data. Now informational only; reduced noise from 137 → 93 (filtered to pre-project-start `first_seen_date` only).
- `validate.py` corp-action matching window widened from ±6 months to ±12 months around universe entry/exit to accommodate long M&A timelines (announcement → record date → effective date).

### Added
- `data/corporate_actions.csv` — seeded with 30 rows documenting 37 universe-exit events. Covers PSU bank mega-merger (ALBK, ANDHRABANK, CORPBANK, DENABANK, ORIENTBANK, SYNDIBANK, UNITEDBNK, VIJAYABANK), HDFC→HDFCBANK, GRUH→BANDHANBNK, BHARATFIN→INDUSINDBK, GSKCONS→HINDUNILVR, MINDTREE→LTIM, SHRIRAMCIT→SHRIRAMFIN, INOXLEISUR→PVRINOX, TATASTLBSL→TATASTEEL, IDFC→IDFCFIRSTB, ISEC→ICICIBANK, UJJIVAN→UJJIVANSFB, TV18BRDCST→NETWORK18, TATAMTRDVR→TATAMOTORS, JETAIRWAYS delisting, HEXAWARE delisting, and others. Includes ISINs and merger ratios where known.

## v0.3.0 — 2026-05-24

### Added
- `fetch_prices.py` — NSE bhavcopy fallback for symbols yfinance returns empty (typically merged/delisted, e.g. HDFC post-merger, JETAIRWAYS, DHFL). After yfinance batch, symbols with no data are routed to a daily-bhavcopy fetch over their `symbol_history.csv` lifetime, then back-adjusted for splits/bonuses/dividends to match yfinance `auto_adjust=True` semantics. New CLI flag `--no-fallback` preserves prior yfinance-only behavior.
- `utils/nse_corpact.py` — Fetches NSE corporate actions (`/api/corporates-corporateActions`) per symbol; parses `subject` strings into structured `{split, bonus, dividend}` events with ratios/amounts. Per-symbol JSON cache in `data/nse_corpact_cache/` (7-day TTL via `NSE_CORPACT_CACHE_DAYS`).
- `utils/price_adjust.py` — Pure-function back-adjustment of OHLCV given an action ledger. Cumulative factor applied to all bars before each ex-date. Volume adjusted inversely on splits/bonuses to match yfinance convention.
- `utils/bhavcopy.py::fetch_daily_range(symbol, start, end)` — Iterates trading days, returns wide OHLCV DataFrame for one symbol, fetches missing days into `bhavcopy_cache/`. Skips weekends; tolerates holiday 404s.
- `data/corporate_action_adjustments.csv` — Flattened ledger of every split/bonus/dividend applied to a fallback symbol. Columns: `symbol, ex_date, action_type, ratio_num, ratio_denom, amount, source, raw_subject`.
- Two new validator checks: `prices_fallback_coverage` (% of `xbrl_unavailable_for_delisted` symbols recovered via fallback) and `corp_action_adjustment_symbol_coverage` (every symbol in adjustments file has rows in `prices_panel.csv`).
- Two new `known_data_gaps.csv` reason codes: `bhavcopy_no_rows_in_range` (symbol absent from bhavcopies in its lifetime range) and `no_corp_actions_for_fallback` (bhavcopy prices kept unadjusted because NSE corp action API returned empty — e.g. TATAMOTORS post-demerger).

### Changed
- `config.py` — added `NSE_CORPACT_CACHE_DIR` and `NSE_CORPACT_CACHE_DAYS = 7`.
- `fetch_prices.py` — flow now: yfinance batch → collect empty/missing symbols → bhavcopy+corp-action fallback → write combined panel. yfinance remains the primary source; fallback is transparent to downstream consumers (long-format panel schema unchanged).

### Notes
- **Source pivot:** original plan called for BSE corporate actions. BSE's `/api/Corpaction/w` ignores the `scripcode` filter (returns 10 latest actions globally regardless of param) — confirmed by probing 15+ endpoint variants. NSE's per-symbol endpoint works cleanly, covers delisted symbols (HDFC: 20 actions, DHFL: 20, RCOM: 13, JETAIRWAYS: 15), and reuses the existing `curl_cffi` session pattern. No security-code lookup needed.
- **TATAMOTORS** still has no price data after this change: NSE purged the symbol from its corp action API post-demerger (returns 0 rows). Recorded with reason `no_corp_actions_for_fallback`. Bhavcopy daily fetch still succeeds, but prices remain unadjusted; for now they are not written to the panel. Future work: extend `corporate_actions.csv` to support manual ratio entries as an override source.
- **Adjustment semantics match yfinance `auto_adjust=True`:** splits + bonuses + cash dividends back-adjusted. Combined panel is consistent across yfinance-sourced and bhavcopy-sourced symbols.
- **Performance:** cold daily bhavcopy fetch for one symbol covers ~1900 trading days at ~0.4–1.0s each = ~15–30 min per fallback symbol. After first run, all parquets cached → seconds. NSE corp action API per symbol = ~1s (cached 7 days).

## v0.2.1 — 2026-05-24

### Fixed
- `universe.py::build_symbol_history_from_bhavcopies` — auto-warms `bhavcopy_cache/` when empty by fetching quarter-end snapshots across `PROJECT_START_DATE..today`. Previously, running `universe.py --bootstrap --refresh-universe` on a cold cache silently produced an empty `symbol_history.csv` (and therefore a `raw_universe.csv` with no historical/delisted symbols like TATAMOTORS, DHFL, RCOM). The bootstrap step is now self-sufficient — no longer requires a prior ranking run to seed the cache.

### Added
- `universe.py::_warm_bhavcopy_cache` — internal helper. Iterates `quarter_end_dates(PROJECT_START_DATE, today)` with the same 7-day-offset fallback used by `compute_universe_history`. No-op for quarter ends already cached, so re-runs stay cheap.

### Changed
- `run_all.py` docstring — removed the "Bootstrap dependency" warning about warming `bhavcopy_cache/` before the first run; no longer applicable.
- `docs/README.md` — removed the "Important" caveat warning users to pre-warm `bhavcopy_cache/` before `universe.py --bootstrap --refresh-universe`. Adjusted bootstrap runtime estimate to reflect the first-run warming cost (~5–10 min on a cold cache, < 1 min on a warm one).

## v0.2.0 — 2026-05-24

### Added
- `data/symbol_history.csv` — every NSE EQ symbol that has ever appeared in any cached bhavcopy parquet, with `first_seen_date`, `last_seen_date`, `status`, `isin`, `source`. Built by `build_symbol_history_from_bhavcopies()` in `universe.py`. Triggered automatically on `--refresh-universe`.
- `data/corporate_actions.csv` — manually curated audit trail of splits / demergers / mergers / renames. Seed rows: TATAMOTORS→TMPV+TMCV (2025), HDFC→HDFCBANK (2023), RELIANCE→JIOFIN (2023), GMRINFRA→GMRAIRPORT (2022), GMRINFRA demerger of GMRPIL (2022). Used by validator checks and lineage CLI; **not consumed by ranking code**.
- `data/known_data_gaps.csv` — populated by `fetch_shp_xbrl.py` at runtime. Records symbols for which NSE's XBRL API returns no filings (e.g. truly-delisted symbols). Columns: `symbol, reason, recorded_date`.
- `utils/lineage.py` — CLI to query corporate action lineage (`python -m utils.lineage --symbol TATAMOTORS`). Reads `corporate_actions.csv` and `symbol_history.csv`.
- Two new validator checks in `validate.py`: `survivorship_bias_exits` (unexplained symbol exits) and `survivorship_bias_entries` (unexplained late entrances), cross-referenced against `corporate_actions.csv`.

### Changed
- `universe.py::build_raw_universe` — union-only; never drops previously-seen symbols. Accepts existing `raw_universe.csv` + live NSE list + Wikipedia + `symbol_history.csv` as sources. Re-running with `--refresh-universe` is idempotent.
- `universe.py::main` — calls `build_symbol_history_from_bhavcopies()` before `build_raw_universe()` when `--refresh-universe` is passed.
- `fetch_shp_xbrl.py` — tracks symbols that returned no XBRL data; writes them to `data/known_data_gaps.csv` with reason `xbrl_unavailable_for_delisted`. Emits WARNING-level log (was silent drop).

### Notes
- After running `python universe.py --bootstrap --refresh-universe`, `raw_universe.csv` will include TATAMOTORS, DHFL, RCOM, JETAIRWAYS and other historically-traded symbols that were absent from the live NSE EQUITY_L.
- `compute_universe_history` is unchanged — the fix is entirely upstream in raw_universe construction.
- Symbols with no XBRL data (e.g. delisted symbols where NSE API is silent) will appear in `raw_universe.csv` and `symbol_history.csv` but be absent from `universe_history.csv`. Recorded in `known_data_gaps.csv`. See docs for known limitations.
- TATAMOTORS XBRL backfill: requires manually running `python fetch_shp_xbrl.py --symbols TATAMOTORS,DHFL,RCOM,JETAIRWAYS,COXANDKINGS` and checking logs. Step 4 of implementation plan.
