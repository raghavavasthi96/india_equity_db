# Changelog (cross-module)

Per-module changelogs:
- [`pipeline/docs/changelog.md`](pipeline/docs/changelog.md) — pipeline (v0.1–v0.4).
- [`backtest/docs/changelog.md`](backtest/docs/changelog.md) — backtest engine (v0.5.0+).

## v0.6.0 — 2026-05-25

Repository split into two side-by-side modules sharing one environment:
`pipeline/` (data ingestion + universe) and `backtest/` (analysis engine).
No functional changes; entirely a layout + import refactor.

### Moves
- Pipeline scripts hoisted into `pipeline/`:
  `universe.py`, `fetch_shp_xbrl.py`, `fetch_prices.py`, `fetch_financials.py`,
  `validate.py`. `run_all.py` renamed to `run_pipeline.py`.
- Pipeline utils → `pipeline/utils/` (`bhavcopy`, `cli`, `dates`, `gaps`, `http`,
  `io`, `lineage`, `logger`, `nse_corpact`, `price_adjust`).
- Pipeline `config.py` → `pipeline/configs/config.py`. `DATA_DIR` / `LOG_DIR`
  rebased on `pipeline/` package root.
- Pipeline data + logs → `pipeline/data/`, `pipeline/logs/`.
- Backtest `config.py` → `backtest/configs/config.py`. Adds `PIPELINE_DATA_DIR`
  (imported from pipeline) and `BACKTEST_DATA_DIR` constants.
- Backtest `data.py` **renamed** to `backtest/context.py` — avoids the
  `backtest.data` module ↔ `backtest/data/` directory namespace collision.
- Backtest data (`nifty500_tri.csv`, `cache/`, `backtests/`) → `backtest/data/`.
- `utils/benchmark.py` → `backtest/utils/benchmark.py`.
- Docs: `docs/README.md` split into per-module READMEs;
  `docs/india_equity_db_plan.md` → `pipeline/docs/architecture.md`;
  `docs/backtest_plan.md` → `backtest/docs/architecture.md`. Root gets a
  1-pager `README.md` and this `CHANGELOG.md`.

### Invocation changes
| Before | After |
|---|---|
| `python universe.py --bootstrap` | `python -m pipeline.universe --bootstrap` |
| `python fetch_shp_xbrl.py` | `python -m pipeline.fetch_shp_xbrl` |
| `python fetch_prices.py` | `python -m pipeline.fetch_prices` |
| `python fetch_financials.py` | `python -m pipeline.fetch_financials` |
| `python validate.py` | `python -m pipeline.validate` |
| `python run_all.py` | `python -m pipeline.run_pipeline` |
| `python -m utils.lineage --symbol X` | `python -m pipeline.utils.lineage --symbol X` |
| `python -m utils.benchmark` | `python -m backtest.utils.benchmark` |
| `python -m backtest.run_backtest backtest/configs/X.py` | unchanged |

### Import surface
- Pipeline submodules: `from pipeline.configs import config`, sibling-relative
  `from .utils.X import Y`.
- Backtest preset files use the public re-export: `from backtest import
  BacktestConfig, momentum_12_1, compose, …`. Internal modules
  (`backtest.engine`, `backtest.context`) are not part of the stable surface.

### Notes
- One-way dependency: `backtest.configs.config` imports from
  `pipeline.configs.config`. Pipeline never imports backtest.
- All smoke tests passing post-refactor:
  - `python -m pipeline.validate` — runs end-to-end, reads pipeline panels.
  - `python -m backtest.run_backtest backtest/configs/default_ffmcap_q.py` —
    writes to `backtest/data/backtests/default_ffmcap_q/`, dashboard renders.
  - `python -c "import backtest; backtest.build_context"` — proves rename +
    re-export wiring.
- Existing `__pycache__/` directories at all old locations were purged to
  prevent stale `.pyc` files from shadowing the new layout (especially
  `backtest/__pycache__/data.cpython-*.pyc` which would mask the new
  `backtest/data/` directory).
