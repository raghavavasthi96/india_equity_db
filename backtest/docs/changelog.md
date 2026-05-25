# Backtest changelog

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
