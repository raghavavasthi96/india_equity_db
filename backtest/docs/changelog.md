# Backtest changelog

## v0.5.4 — 2026-08-15

Sector caps, breakdown and attribution activated by the new pipeline
classification (`pipeline/docs/changelog.md` v0.4.5).

### Added
- **`attribution.py`** — `sector_weights`, `sector_contribution`,
  `sector_active_weights`, `build_sector_frames` (memoised on the result so the
  writer and dashboard share one computation), and `verify_identities`.
- **Two new outputs** per run: `sector_attribution.csv`
  (`date, sector, weight, contrib_gross, contrib_tcost, contrib_net,
  cum_contrib_net_pct`) and `sector_active_weights.csv`
  (`date, sector, portfolio_wt, benchmark_wt, active_wt`).
- **`cfg.sector_level`** — pick the grouping level (`macro_sector` / `sector` /
  `industry` / `basic_industry`); default `sector` (22 buckets).
- **`context.benchmark_weights_asof`** — the PIT ff-mcap benchmark weight vector,
  extracted from `dashboard._active_weights_fig` so the stock-level and
  sector-level active weights can't drift apart.
- **`summary.json`** gains `sector_level`, `sector_contribution_net`,
  `sector_active_wt_end`, `sector_attribution_residuals`.
- **Dashboard**: sector weight area chart, cumulative net contribution by sector,
  and active sector tilt vs ff-mcap top-500 replace the old
  "Sector breakdown unavailable" note. Header band gains a max-sector-weight cell
  alongside the existing max-stock-weight one, labelled with the active
  `sector_level`.
- **`rebalance_diagnostics.csv`** gains a `sector` column.

### Changed
- `cfg.sector_map_csv` now defaults to the pipeline's
  `sector_classification.csv` when `None`, so existing presets light up with no
  edits. A bare two-column `symbol,sector` CSV is still accepted.
- `weights.apply_caps` raises on an infeasible sector cap
  (`max_sector_wt × n_sectors < 1`), mirroring the existing stock-cap guard,
  instead of spinning to the 20-iteration non-convergence error.

### Notes
- Attribution reconciles to the engine exactly (residuals ~1e-15): earning
  weights come from `post_cap_weight` on trade dates rather than a naive
  `shift(1)`, net mirrors the engine's `(1+gross)(1−drag)−1` NAV haircut rather
  than `gross−drag`, and cumulative contribution is accumulated in NAV units.
- Active sector weights are vs the PIT ff-mcap universe, **not** the Nifty 500 —
  the TRI file carries index levels only, never constituents. Charts are labelled
  accordingly.
- Sector labels are a current snapshot, not point-in-time.

## v0.5.3.1 — 2026-08-15

Niftyindices endpoint repair and a config default correction.

### Fixed
- **Nifty 500 TRI fetch was broken** ([`utils/benchmark.py`](../utils/benchmark.py)).
  Niftyindices moved off the ASMX-style endpoint: `Backpage.aspx/getTotalReturnIndexString`
  → `BackPage/getTotalReturnIndexString`, and the response is now a bare JSON
  array rather than `{"d": "<json string>"}`. Both the URL and the unwrapping
  (`rows = json.loads(outer["d"])` → `rows = r.json()`) updated. Re-seed with
  `python -m backtest.utils.benchmark --start 2018-01-01`.

### Changed
- **`BacktestConfig.end` default `"2026-04-30"` → `None`**, so runs extend to the
  last trading date in the price panel instead of silently truncating at a
  hardcoded date that ages out. Set `end` explicitly to pin a window.

## v0.5.3 — 2026-05-25

Two follow-up fixes shaken out by the v0.4.2 price-history extension.

### Fixed
- **Snap-to-prior-trading-day boundary against the first universe snapshot**
  ([`context.py:build_rebal_schedule`](../context.py)). With the prices panel
  now extending below `PROJECT_START_DATE`, the Q rebal candidate `2018-06-30`
  (Saturday) snapped back to `2018-06-29` (Friday — previously unavailable, now
  a valid trading day). But `snap_asof` is strict `≤` and the first universe
  snapshot is dated `2018-06-30`, so the rebal produced an empty pool. Schedule
  now detects "snap-backward landed strictly before the first universe
  snapshot" and falls forward to the first trading day `≥` candidate, landing
  on `2018-07-02` in this case. Same first-day seed across all rebal
  frequencies sharing the same `cfg.start`.
- **Engine crash when a rebal produces an empty target**
  ([`engine.py:run_backtest`](../engine.py)). `trade_dates_set` was built from
  the full schedule, but `targets` only held rebals that produced non-empty
  weights. A skipped rebal then tripped `KeyError: Timestamp(...)` in the daily
  loop on its trade date. Defensive fix: derive `trade_dates_set` from
  `targets.keys()` after the pre-compute pass.

## v0.5.2 — 2026-05-25

Fundamental-signal warm-up runway.

### Fixed
- **`earnings_growth_yoy` first-year coverage**: paired with pipeline v0.4.3's
  `FINANCIALS_HISTORY_LOOKBACK_QUARTERS` (defaults to 6). `financials_panel.csv`
  and `financials_annual_panel.csv` now extend ~18 months before
  `PROJECT_START_DATE`, so YoY / multi-quarter fundamental signals have a
  prior-year reading at the first rebal date instead of returning an empty
  score for the first ~14 months of backtest.

## v0.5.1 — 2026-05-25

Cross-frequency comparability + cleaner dashboard stats.

### Fixed
- **Schedule snap falls forward when no prior trading day exists**
  ([`context.py:build_rebal_schedule`](../context.py)). Previously, a calendar
  candidate that pre-dated the price panel (e.g. `cfg.start = 2018-06-30` when
  `prices_panel.csv` started at `2018-07-02`) was silently dropped — pushing
  the next candidate to become `schedule[0]`. This produced different effective
  start dates across rebal frequencies sharing the same `cfg.start`: M anchored
  on `2018-07-31`, Q on `2018-09-28`, so each run loaded a different benchmark
  window and reported different `cagr_benchmark` / `vol_benchmark` /
  `sharpe_benchmark`. After the fix, both frequencies seed on the first
  available trading day, and the benchmark profile is identical across all
  rebal frequencies for a given `cfg.start`.
- **Signal warm-up runway**: paired with pipeline v0.4.2's
  `PRICE_HISTORY_LOOKBACK_DAYS` (defaults to 365). `prices_panel.csv` now
  extends ~1 year before `PROJECT_START_DATE`, so signals with trailing
  lookbacks (`momentum_12_1`, `vol_inverse`, …) have data at the first rebal
  date instead of producing an empty score and tripping the
  "ffmcap_tilt requires signal_fn" guard in `weights.py`.

### Changed
- **`metrics.compute_summary`** — `avg_one_way_turnover` renamed to
  `turnover_per_rebal`; new `turnover_annualized` (= `Σ one-way turnover / years`,
  matching the `tcost_bps_annualized` formulation).
- **Dashboard stats cards** — dropped Gross max DD and Benchmark max DD blocks
  (the drawdown overlay chart still shows all three series; the table now
  surfaces only the Net DD block to declutter). Portfolio card now shows
  "Turnover / rebal" + "Turnover annualised" alongside the existing T-cost pair.

### Preset tuning (non-breaking)
- `momentum_q` and `mom_lvol_q`: `tilt_gamma 1.0 → 1.5`, switched post-signal
  selection from `top_quantile=0.3` to `top_n=50`. `mom_lvol_q` renamed to
  `mom_lvol_q_top50` via `run_id`.

## v0.5.0 — 2026-05-25

First analysis layer on top of the database: a flexible, PIT-correct,
survivorship-bias-free backtest engine with HTML dashboard.

### Added
- **`backtest/` module** — 10 files (~1.6k LOC):
  - `configs/config.py` — `BacktestConfig` + `TcostConfig` dataclasses. Per-field inline
    descriptions for every knob.
  - `context.py` — wide-pivot parquet cache (`backtest/data/cache/close_wide.parquet`,
    `volume_wide.parquet`, `adv_30d_inr.parquet`), `BacktestContext` aggregator,
    `snap_asof()` PIT lookup, rebalance schedule + trade-date helpers.
  - `universe.py` — 6 pre-signal filters (rank / price / liquidity / history /
    free-float / blacklist) + post-signal selectors (`top_n` / `top_quantile`),
    each composable as `(pool, date, ctx) → pool` callables.
  - `signals.py` — `standardise()` (winsorise → z-score → re-z) + three stub
    signals (`momentum_12_1`, `vol_inverse`, `earnings_growth_yoy`) registered
    in `SIGNAL_REGISTRY`.
  - `weights.py` — four weighting schemes (`equal`, `ffmcap`, `score_weighted`,
    `ffmcap_tilt`) + iterative water-filling cap solver (stock → sector → size,
    fixed-point with 20-iter outer cap) + `apply_cash_buffer`.
  - `tcost.py` — composite illiquidity z-score per name (mcap + ADV + free-float
    each negated → log10 → z → summed → re-z) mapped to bps via
    `base + k_illiq × max(0, z)`, clipped. PIT inputs sampled at **trade date**.
  - `engine.py` — main rebalance loop: PIT snapshot → filter → signal → select
    → weight → caps → cash buffer → t-cost drag → daily buy-and-hold drift.
    Synthetic `CASH` ticker (r=0) absorbs cash buffer + delisting proceeds for
    clean weight-sum-to-1 accounting.
  - `metrics.py` — CAGR / vol / Sharpe / Sortino (all gross/net/bench),
    drawdown series + max DD start/end/recovery, hit rate, TE, IR, calendar-year
    returns, avg turnover, t-cost bps annualised.
  - `dashboard.py` — single self-contained Plotly HTML report (dark theme).
    Sections: header band, performance (equity / active equity / drawdown /
    calendar bars), grouped stats cards (Returns / Risk / Active / Drawdown /
    Portfolio), risk & turnover (720-px rolling 4-panel, turnover bars,
    t-cost bars, position count), sector & size area charts, composition
    (top holdings, active over/under, concentration with HHI). Custom HTML
    yaxis toggle buttons (linear/log) with `Plotly.relayout`.
  - `run_backtest.py` — CLI entry. Writes outputs to
    `backtest/data/backtests/<run_id>/`. Auto-opens dashboard when the preset
    sets `auto_open = True`.
- **`backtest/utils/benchmark.py`** — Nifty 500 Total Returns Index fetcher + loader.
  Pulls from niftyindices.com's `getTotalReturnIndexString` endpoint via
  `curl_cffi` (NSE bot guard), chunked 360-day requests. Writes
  `backtest/data/nifty500_tri.csv`. Run as `python -m backtest.utils.benchmark --start 2018-01-01`.
- **`backtest/docs/architecture.md`** — full design doc (17 sections, ~7k words) covering
  scope, inputs, data flow, rebalance loop pseudocode, the `BacktestConfig`
  reference, cap-solver semantics, t-cost formula, benchmark, validation gates,
  and deferred items.

### Notes
- **Default benchmark**: `nifty500_tri`. Required file
  `backtest/data/nifty500_tri.csv` is **not** auto-seeded by `pipeline.run_pipeline`;
  run `python -m backtest.utils.benchmark --start 2018-01-01` once before the
  first backtest (or set `BacktestConfig.benchmark_csv` to override). Engine
  raises with clear instructions if the file is missing.
- **Validation gates passing**: replication preset
  (`weighting=ffmcap`, all filters/caps off, `tcost=0`) correlates
  ~1.0 with the loaded Nifty 500 TRI over overlapping window; sum of
  daily weights = 1 within `1e-9`; no look-ahead (T+1 trade offset,
  ADV at trade date); delistings liquidate into `CASH` and persist.
- **Sector caps** stubbed only — `BacktestConfig.max_sector_wt` reads
  `sector_map_csv` if provided, logs a warning and no-ops otherwise.
  `metadata.csv` sector field currently empty.
- **Output format**: `weights.csv` and `rebalance_diagnostics.csv` are
  CSVs (wide-format weights × dates can be ~50 MB on full-window runs;
  swap to parquet by editing `run_backtest.write_outputs` if disk space
  matters).

> Note: paths and module names in this entry reflect the post-v0.6.0 layout
> (flat `backtest/`, `data.py` → `context.py`). The v0.5.0 release shipped
> under the pre-refactor layout (`backtest/data.py`, `data/backtests/`,
> `python -m utils.benchmark`); see the root `CHANGELOG.md` v0.6.0 entry for
> the rename + move map.
