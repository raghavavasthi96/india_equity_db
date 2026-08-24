# Backtest Engine

Flexible, point-in-time, survivorship-bias-free backtest engine on top of the
[india_equity_db pipeline](../../pipeline/docs/README.md). Renders a self-contained
HTML dashboard per run.

Reads pipeline data via `pipeline.configs.config.DATA_DIR`. One-way dependency
only — pipeline never imports backtest.

---

## Setup

Requires the pipeline data to already be built (run the pipeline first, see the
pipeline README). Then seed the benchmark once:

```bash
# Fetch / refresh Nifty 500 Total Returns Index from niftyindices.com (default)
python -m backtest.utils.benchmark --start 2018-01-01

# Nifty500 Momentum 50 TRI
python -m backtest.utils.benchmark --index nifty500_momentum50_tri --start 2018-01-01

# Custom output path
python -m backtest.utils.benchmark --start 2018-01-01 --out backtest/data/my_benchmark.csv
```

Writes one CSV per index under `backtest/data/` (columns: `date,close`), named
by the `BENCHMARKS` registry in `backtest/utils/benchmark.py`:
`nifty500_tri.csv` and `nifty500_momentum50_tri.csv`. Select which one a run
compares against with `BacktestConfig.benchmark`. Chunked 360-day requests via
`curl_cffi` to bypass NSE's bot guard. Re-run periodically to keep the series
current.

---

## Backtest CLI

Driven by Python preset files under `backtest/configs/`. Each preset is a small,
self-contained file defining `cfg = BacktestConfig(...)`. Run by passing the
preset path as the only argument:

```bash
# Default benchmark-like portfolio (ff-mcap, quarterly, 20% cap)
python -m backtest.run_backtest backtest/configs/default_ffmcap_q.py

# 12-1m momentum, ff-mcap tilted, top 30%, 10% stock cap
python -m backtest.run_backtest backtest/configs/momentum_q.py

# Composite: momentum + low-volatility (demonstrates compose() API)
python -m backtest.run_backtest backtest/configs/mom_lvol_q.py
```

To create a new strategy: copy [../configs/template.py](../configs/template.py) —
every knob is listed with its default — uncomment what you want to override,
save, run.

Presets should import the public API:

```python
from backtest import BacktestConfig, TcostConfig, momentum_12_1, compose
```

(Internal modules like `backtest.engine` / `backtest.context` can be imported
directly but are not part of the stable surface.)

---

## Outputs

Outputs are written to `backtest/data/backtests/<run_id>/`:

| File | Description |
|---|---|
| `config.json` | Run config + input file fingerprints (mtime + size) for reproducibility. |
| `weights.csv` | Wide format (date × symbol) of held weights, including the synthetic `CASH` ticker. Can be ~50 MB on full-window runs. |
| `rebalance_diagnostics.csv` | Per-rebalance: pool size, turnover, gross/net cost, top concentrations. |
| `returns.csv` | Daily gross/net/benchmark returns. |
| `turnover.csv` | Per-rebalance one-way turnover (sum of \|Δw\|/2). |
| `tcost.csv` | Per-rebalance gross-NAV t-cost drag in bps. |
| `sector_attribution.csv` | Daily `date, sector, weight, contrib_gross, contrib_tcost, contrib_net, cum_contrib_net_pct`. Reconciles exactly to `returns.csv` and `tcost.csv`. |
| `sector_active_weights.csv` | Daily `date, sector, portfolio_wt, benchmark_wt, active_wt` vs the PIT ff-mcap universe (**not** the Nifty 500 — the TRI file has no constituents). |
| `summary.json` | Headline stats (CAGR, vol, Sharpe, max DD, IR, TE, etc.) plus `sector_contribution_net`, `sector_active_wt_end` and `sector_attribution_residuals`. |
| `dashboard.html` | Self-contained Plotly report (dark theme). Open in any browser. |

Engine intermediates cached under `backtest/data/cache/`:
- `close_wide.parquet`, `volume_wide.parquet`, `adv_30d_inr.parquet` —
  wide pivots of `pipeline/data/prices_panel.csv`. Auto-rebuilt on source
  mtime change.

## Sector grouping

Sector caps, the dashboard sector breakdown and sector attribution all read
`pipeline/data/sector_classification.csv`, produced by
`python -m pipeline.fetch_sectors`. Nothing needs configuring — presets pick it
up automatically. Two knobs:

```python
cfg = BacktestConfig(
    sector_level="sector",   # macro_sector (12) | sector (22) | industry (~55) | basic_industry (~152)
    max_sector_wt=0.25,      # per-sector cap; raises if max_sector_wt x n_sectors < 1
)
```

If the classification file is missing, the sector cap logs a warning and no-ops,
the sector outputs are skipped, and the dashboard prints a note — everything else
runs unchanged.

See [architecture.md](architecture.md) for the full design and config reference.
