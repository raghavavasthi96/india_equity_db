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
# Fetch / refresh Nifty 500 Total Returns Index from niftyindices.com
python -m backtest.utils.benchmark --start 2018-01-01

# Custom output path
python -m backtest.utils.benchmark --start 2018-01-01 --out backtest/data/my_benchmark.csv
```

Writes `backtest/data/nifty500_tri.csv` (columns: `date,close`). Chunked
360-day requests via `curl_cffi` to bypass NSE's bot guard. Re-run periodically
to keep the series current.

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
| `summary.json` | Headline stats (CAGR, vol, Sharpe, max DD, IR, TE, etc.). |
| `dashboard.html` | Self-contained Plotly report (dark theme). Open in any browser. |

Engine intermediates cached under `backtest/data/cache/`:
- `close_wide.parquet`, `volume_wide.parquet`, `adv_30d_inr.parquet` —
  wide pivots of `pipeline/data/prices_panel.csv`. Auto-rebuilt on source
  mtime change.

See [architecture.md](architecture.md) for the full design and config reference.
