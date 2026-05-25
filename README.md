# india_equity_db

Two side-by-side Python modules sharing one environment:

- **[`pipeline/`](pipeline/docs/README.md)** — survivorship-bias-free database of NSE
  top-500 companies (Mar 2018+). Outputs long-format CSVs for prices, financials,
  shares outstanding, and quarterly universe history.
- **[`backtest/`](backtest/docs/README.md)** — flexible, point-in-time backtest
  engine layered on the pipeline data. Renders a self-contained HTML dashboard
  per run.

`backtest/` reads pipeline data via `pipeline.configs.config.DATA_DIR`. One-way
dependency only — pipeline never imports backtest.

## Quick start

```bash
pip install -r requirements.txt

# Build / refresh the database (full 500-symbol pull)
python -m pipeline.run_pipeline --mode full

# Run the default benchmark-like backtest
python -m backtest.utils.benchmark --start 2018-01-01     # one-off seed
python -m backtest.run_backtest backtest/configs/default_ffmcap_q.py
```

See per-module READMEs for full CLI / config / output references:

- [`pipeline/docs/README.md`](pipeline/docs/README.md) — pipeline how-to.
- [`pipeline/docs/architecture.md`](pipeline/docs/architecture.md) — pipeline design.
- [`backtest/docs/README.md`](backtest/docs/README.md) — backtest how-to.
- [`backtest/docs/architecture.md`](backtest/docs/architecture.md) — backtest design.

## Changelog

Cross-module entries: [`CHANGELOG.md`](CHANGELOG.md).
Per-module: [`pipeline/docs/changelog.md`](pipeline/docs/changelog.md),
[`backtest/docs/changelog.md`](backtest/docs/changelog.md).
