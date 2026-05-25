# Backtest Engine — Design

Status: shipped in v0.5.0; module layout updated in v0.6.0 refactor (flat `backtest/`,
`data.py` → `context.py`, data isolated to `backtest/data/`).

## 1. Scope & guiding principles

Build a flexible, PIT-correct, survivorship-bias-free backtest engine on top of the existing `india_equity_db` outputs.

- Default run: quarterly-rebalanced free-float market-cap-weighted portfolio of the PIT top-500 universe.
- All knobs exposed via a single `BacktestConfig` dataclass.
- Prefer simplicity. No look-ahead. Symbol is primary key everywhere (consistent with `[[project-india-equity-db]]`).

## 2. Inputs

Pipeline-sourced data is read via `pipeline.configs.config.DATA_DIR` (resolves
to `pipeline/data/`). Backtest-owned data lives under `backtest/data/`.

| File | Use |
|---|---|
| `pipeline/data/universe_history.csv` | PIT membership, rank, mcap, free-float mcap/shares — quarterly snapshots |
| `pipeline/data/prices_panel.csv` | Long-format OHLCV. Already total-return adjusted (yfinance `auto_adjust=True` + bhavcopy fallback) |
| `pipeline/data/shares_outstanding.csv` | Total / promoter / public / free-float share counts (PIT quarterly) |
| `pipeline/data/financials_panel.csv` | Quarterly fundamentals for signals |
| `pipeline/data/metadata.csv` | Sector field currently empty — sector caps deferred |
| `backtest/data/sector_map.csv` *(optional, future)* | `symbol,sector` mapping when populated |
| `backtest/data/nifty500_tri.csv` | Nifty 500 Total Returns Index, fetched via `python -m backtest.utils.benchmark` |

Prices are total-return adjusted upstream, so daily `close.pct_change()` gives total returns. No further CA handling.

## 3. Module layout

Top-level `india_equity_db/backtest/`:

| Module | Purpose |
|---|---|
| `configs/config.py` | `BacktestConfig` + `TcostConfig` dataclasses; all knobs |
| `context.py` | Loaders + parquet cache (long→wide pivot done once), `BacktestContext`, PIT snapshot helpers |
| `universe.py` | Composable PIT eligibility filters |
| `signals.py` | Signal protocol, standardisation pipeline, stubs (momentum, vol, earnings growth), `compose` |
| `weights.py` | Four weighting schemes + cap solver |
| `tcost.py` | Per-name liquidity-driven cost model |
| `engine.py` | Rebalance loop, daily drift, return accrual |
| `metrics.py` | Performance stats |
| `dashboard.py` | Self-contained Plotly HTML report |
| `run_backtest.py` | Loads a preset Python file, runs the engine, writes outputs under `backtest/data/backtests/<run_id>/` |
| `configs/*.py` | Preset config files (`cfg = BacktestConfig(...)`). Includes `template.py`, `default_ffmcap_q.py`, `momentum_q.py`, `mom_lvol_q.py`. |
| `utils/benchmark.py` | CLI Nifty 500 TRI fetcher (niftyindices.com) |

Public surface re-exported from `backtest/__init__.py`: `BacktestConfig`,
`TcostConfig`, `run_backtest`, `build_context`, `compose`, `standardise`,
`momentum_12_1`, `vol_inverse`, `earnings_growth_yoy`, `mom_lvol`,
`SIGNAL_REGISTRY`. Preset files should import from `backtest`; internal modules
(`backtest.engine`, `backtest.context`) are not part of the stable surface.

## 4. Data flow

1. Pivot `pipeline/data/prices_panel.csv` long → wide `close` (date × symbol), cache `backtest/data/cache/close_wide.parquet`. Mtime-checked.
2. Same pivot for `volume`; precompute `adv_30d_inr = rolling 30d median(close * volume)`.
3. `universe_history` indexed by `quarter_end_date`; `asof(rebal_date)` returns the live PIT snapshot.
4. Calendar = union of trading dates in `close`.
5. Daily returns = `close.pct_change()`. Missing close → return treated as 0 that day; standard buy-and-hold drift (§5) handles the rest. The real price move is captured on the next traded day. Delisted between rebalances → liquidate at last available close into the synthetic `CASH` ticker (return = 0), held to next rebalance.

Cash is a first-class member of the weight vector via a synthetic symbol `CASH` with `r=0`. This makes every accounting identity (`Σ w = 1`, `cash_buffer`, delisting proceeds) flow through the same code path with no special cases.

## 5. Rebalance loop

```
w_drifted = {CASH: 1.0}                                   # initial holding before first trade

for rebal_date in rebal_schedule:
    trade_date = rebal_date + rebal_offset                # default T+1
    snap    = universe_history.asof(rebal_date)           # PIT snapshot
    pool    = universe_filter(snap, rebal_date, ctx)      # pre-signal filters
    score   = signal_fn(pool, rebal_date, ctx)            # None → no signal
    pool    = post_signal_select(pool, score, cfg)        # top_n / top_quantile
    w_raw   = weighting_fn(pool, score, snap, cfg)        # equal | ffmcap | score_weighted | ffmcap_tilt
    w_tgt   = apply_caps(w_raw, cfg, sector_map, size_map) # iterative water-fill (§11)
    w_tgt   = apply_cash_buffer(w_tgt, cfg)               # scale to 1 - cash_buffer, CASH = remainder
    tc_drag = tcost(w_tgt - w_drifted, trade_date, ctx)   # bps on |Δw| using PIT illiquidity
    apply tc_drag to NAV on trade_date
    w_drifted = w_tgt                                     # carry forward to next rebal

between rebalances (daily t):
    port_r[t] = (w[t-1] * r[t]).sum()                     # CASH contributes 0
    w[t]      = (w[t-1] * (1 + r[t])) / (1 + port_r[t])   # buy-and-hold drift
```

Trading convention: snapshot at `rebal_date`, trade at `rebal_date + rebal_offset` (default T+1) at close. T-cost inputs (mcap, ADV, ff%) sampled at `trade_date`, not `rebal_date`.

## 6. `BacktestConfig`

| Field | Default | Purpose |
|---|---|---|
| `start`, `end` | full range | Backtest window |
| `rebal_freq` | `"Q"` | `M` / `Q` / `A` / list of custom dates |
| `rebal_offset` | `1` | Trade T+offset after snapshot |
| `weighting` | `"ffmcap"` | `equal` / `ffmcap` / `score_weighted` / `ffmcap_tilt` |
| `tilt_gamma` | `0.5` | γ for `ffmcap_tilt` |
| `score_threshold` | `0.0` | Floor for `score_weighted` |
| `signal_fn` | `None` | Callable `(pool, date, ctx) → Series` |
| `signal_top_n` / `signal_top_quantile` | `None` | Post-signal universe filter (see §7) |
| `winsor_sigma` | `3.0` | Signal winsorisation |
| `max_stock_wt` | `0.20` | Per-name cap |
| `max_sector_wt` | `None` | Stub — needs sector map |
| `sector_map_csv` | `None` | Path to `symbol,sector` CSV; required for sector caps |
| `max_size_bucket_wt` | `None` | Dict `{bucket_name: cap}`, e.g. `{"small": 0.20}` |
| `size_buckets` | `{"large": (1, 100), "mid": (101, 250), "small": (251, None)}` | PIT-rank-based bucket boundaries |
| `min_price` | `1.0` | INR floor |
| `min_adv_inr` | `None` | Absolute liquidity filter |
| `min_history_days` | `0` | Listed-history floor |
| `max_universe_rank` | `None` | e.g. top 200 of 500 |
| `min_free_float_pct` | `None` | Tight-float filter |
| `exclude_symbols` | `[]` | Hard blacklist |
| `tcost` | `TcostConfig(...)` | See §8 |
| `cash_buffer` | `0.0` | Post-cap residual; weights normalised to `1 - cash_buffer`, remainder held in `CASH` |
| `benchmark` | `"ffmcap_top500"` | Internal benchmark name |
| `rolling_window_days` | `252` | Window for rolling stats in dashboard (return, vol, Sharpe, IR) |

## 7. Universe filters

`universe.py` exposes stack-composable callables `(pool_df, date, ctx) → pool_df`:

**Pre-signal filters** (run on raw PIT pool):
- `rank_filter` (top-N from `universe_history`)
- `price_filter` (`close ≥ min_price`)
- `liquidity_filter` (`adv_30d_inr ≥ min_adv_inr`)
- `history_filter` (listed ≥ N days)
- `free_float_filter`
- `blacklist_filter`
- Plus any user callables appended via `cfg.extra_filters`.

**Post-signal selection** (runs after `signal_fn`, before weighting):
- `signal_top_n_filter` — keep top N by z-score
- `signal_top_quantile_filter` — keep top quantile by z-score

Selection lives in `universe.py` rather than baked into the weighting fn so it composes with all four weighting schemes (including `ffmcap_tilt`, where "top quintile, ff-mcap tilted" is the natural use case).

## 8. T-cost model

`TcostConfig(base, k_illiq, floor_bps, ceiling_bps)`. Defaults: `base=5, k_illiq=8, floor=3, ceiling=50`.

Single composite illiquidity z-score per symbol, computed cross-sectionally over the eligible pool at **trade date** (not snapshot date, to avoid stale ADV):

```
illiq_raw(sym, t) = z(-log10(mcap_sym_t))
                  + z(-log10(adv_30d_sym_t))      # ADV as of trade date
                  + z(-ff_pct_sym_t)
illiq_z(sym, t)   = z(illiq_raw)                   # re-standardise
cost_bps(sym, t)  = clip(base + k_illiq * max(0, illiq_z), floor, ceiling)
```

Three inputs are highly correlated cross-sectionally (large caps have high ADV and high float). Collapsing to one composite avoids triple-counting the same latent illiquidity and leaves a single coefficient (`k_illiq`) to tune.

Applied on rebalances: `tc_drag_t = Σ_i |Δw_i| * cost_bps_i / 10000`. One-way bps (each leg of buy/sell pays its own cost on its `|Δw|`).

## 9. Signal pipeline

```
raw = signal_fn(pool, date, ctx)                # any units, indexed by symbol
↓ winsorize at ±winsor_sigma
↓ cross-sectional z-score within pool
↓ optional post-signal selection (signal_top_n / signal_top_quantile)
↓ feed to weighting fn
```

Standardisation makes signals on different scales (momentum %, vol ratio, EPS growth %, etc.) directly comparable. Stub signals shipped: `momentum_12_1`, `vol_inverse`, `earnings_growth_yoy`.

**Composite signals** (`signals.compose`): wrap multiple sub-signals into a single `signal_fn`. Each sub-signal is standardised before being weighted-summed on the union of returned symbols (missing → 0 = neutral). The engine then re-standardises the composite output downstream. Negative weights are allowed (invert).

```python
from backtest.signals import compose, momentum_12_1, vol_inverse
combo = compose((momentum_12_1, 0.6), (vol_inverse, 0.4))   # weighted
combo = compose(momentum_12_1, vol_inverse)                  # equal-weight shorthand
```

CLI shorthand via comma-separated spec, optional `:weight` per name:
- `--signal momentum_12_1` — single
- `--signal momentum_12_1,vol_inverse` — equal-weighted composite
- `--signal momentum_12_1:0.6,vol_inverse:-0.4` — explicit weights

Pre-registered composite: `mom_lvol` (50/50 momentum + low-vol).

## 10. Weighting schemes

| Mode | Formula | Signal required? |
|---|---|---|
| `equal` | `w_i = 1/N` | no |
| `ffmcap` | `w_i ∝ ff_mcap_i` *(default)* | no |
| `score_weighted` | `w_i ∝ max(0, z_i - score_threshold)` | **yes** — raises if `signal_fn is None` |
| `ffmcap_tilt` | `w_i ∝ ff_mcap_i * exp(tilt_gamma * z_i)` | **yes** |

Caps applied uniformly *after* weighting via `apply_caps()`, regardless of mode. Cash buffer applied as the final step: investable weights scaled to `1 - cash_buffer`, remainder allocated to `CASH`.

## 11. Cap solver

Inner routine (per cap type): clip names above cap → redistribute excess pro-rata to uncapped → repeat to fixed point.

Outer loop: run inner routine on stock → sector → size sequentially. After all three pass, recheck all three. Repeat the full (stock, sector, size) sequence until no cap fires in a full pass, max 20 outer iterations. Raise on non-convergence. Infeasible configs (e.g. `max_stock_wt * N < 1`) raise immediately.

Sector cap is a stub: reads `cfg.sector_map_csv` if set; if absent, logs a warning and no-ops.

Size buckets defined by `cfg.size_buckets` (rank ranges, inclusive bounds). PIT mcap rank from `universe_history.rank` at the snapshot date.

## 12. Benchmark

`nifty500_tri`: externally-sourced Nifty 500 Total Returns Index, loaded from `backtest/data/nifty500_tri.csv` (columns: `date,close`). Daily returns = `close.pct_change()`, reindexed and forward-filled onto the backtest calendar.

Seed/refresh with `python -m backtest.utils.benchmark --start 2018-01-01`, which fetches from niftyindices.com's `getTotalReturnIndexString` endpoint (chunked 360-day requests, curl_cffi for NSE bot guard) and writes the CSV.

`BacktestConfig.benchmark_csv` overrides the default path if needed (e.g. to point at a custom index file).

## 13. Outputs

Per run, written to `backtest/data/backtests/<run_id>/`:

| File | Contents |
|---|---|
| `config.json` | Frozen config + input file mtimes/hashes |
| `weights.csv` | Daily weights panel (incl. `CASH`), wide format (date × symbol) |
| `rebalance_diagnostics.csv` | Per rebal_date × symbol: `{raw_weight, post_cap_weight, drift_weight, delta_weight, cost_bps}` |
| `returns.csv` | Gross, net, benchmark, active (daily) |
| `turnover.csv` | Per-rebalance one-way turnover |
| `tcost.csv` | Per-rebalance t-cost drag (bps + INR). Aggregate of the per-name `cost_bps × |Δw|` rows in `rebalance_diagnostics.parquet` — kept separate for quick PM consumption. |
| `summary.json` | All headline stats (§14) |
| `dashboard.html` | Self-contained Plotly report (§15) |

Drawdown series and calendar-year returns are derived from `returns.csv` on-the-fly inside the dashboard rather than persisted separately.

## 14. `summary.json` contents

CAGR (gross / net / bench), annualised vol (gross / net / bench), Sharpe (gross / net / bench), Sortino (net), hit rate, avg N positions, avg turnover, **tcost_bps_annualized**, tcost_bps_per_rebal, tracking error, **information ratio**, **calendar_year_returns** (dict by year: gross / net / bench / active).

Drawdown stats reported for all three series (gross portfolio, net portfolio, benchmark): `{series}_max_drawdown`, `{series}_max_drawdown_start`, `{series}_max_drawdown_end`, `{series}_max_drawdown_recovery_days`.

## 15. HTML dashboard

`backtest/dashboard.py` writes a single self-contained Plotly HTML to `dashboard.html`. Sections, top to bottom:

- **Header band** — run ID, dates, rebal freq, weighting, signal, key caps.
- **Performance** — equity curve (gross / net / bench, log toggle); active equity; drawdown overlay (gross / net / bench); calendar-year grouped bars.
- **Stats table** — every field in `summary.json`.
- **Risk & turnover** — rolling return / vol / Sharpe / IR (4-panel, window = `cfg.rolling_window_days`, default 252); turnover bars per rebal; t-cost bars per rebal (bps); position count over time.
- **Composition** — top 20 current holdings; top 10 over/under active weights vs benchmark; cumulative weight concentration curve with HHI in subtitle.
- **Provenance footer** — input mtimes, code version, generated timestamp.

Invocation: presets are Python files under `backtest/configs/`, each defining `cfg = BacktestConfig(...)` and an optional `auto_open = True`. CLI takes a single positional path:

```
python -m backtest.run_backtest backtest/configs/momentum_q.py
```

To create a new strategy: copy `backtest/configs/template.py`, override the knobs you care about, save, run. The dataclass defaults handle everything else.

## 16. Validation gates

Before declaring the engine live:

1. **Replication preset** (`weighting=ffmcap`, quarterly, **all filters disabled, all caps disabled, `cash_buffer=0`, `tcost=0`**) must correlate >0.95 with the loaded `nifty500_tri` benchmark over overlapping window. Some drift is expected (PIT universe ≠ NSE's exact Nifty 500 reconstitution, rebalance timing differs). The default *user-facing* config is not a replication target — it has caps and filters that intentionally deviate from the index.
2. Sum of daily weights (including `CASH`) = 1 every day, within floating-point tolerance: `|Σw − 1| < 1e-9`.
3. No look-ahead: weights on date `t` derive only from data with timestamp ≤ `t - rebal_offset`. T-cost inputs (ADV) also respect this — computed as of trade date, not later.
4. No survivorship: symbols dropped from `universe_history` between rebals continue to accrue returns through delisting date, then liquidate into `CASH`.

## 17. Open / deferred

- Sector caps stubbed; activate when `backtest/data/sector_map.csv` populated.
- Borrow/short modelling not in scope (long-only).
- `CASH` earns 0; configurable cash-rate field can be added on request.
