"""Fully-commented template — copy this file to start a new backtest preset.

Every BacktestConfig field is listed below with its default. Delete the lines
you don't need to override; the dataclass defaults will be used.

Run:
    python -m backtest.run_backtest backtest/configs/template.py
"""

from backtest import BacktestConfig, TcostConfig

# Uncomment to use a signal (single, registered composite, or compose() output)
# from backtest import (
#     momentum_12_1, vol_inverse, earnings_growth_yoy, mom_lvol, compose,
# )
# my_signal = compose((momentum_12_1, 0.6), (vol_inverse, 0.4), name="my_blend")


cfg = BacktestConfig(
    # ----- Window -----
    # start="2018-06-30",
    # end="2026-04-30",

    # ----- Rebalance -----
    # rebal_freq="Q",          # "M" / "Q" / "A" / "custom"
    # custom_rebal_dates=None, # list of date strings if rebal_freq="custom"
    # rebal_offset=1,          # T+1 trade execution

    # ----- Weighting -----
    # weighting="ffmcap",      # "equal" / "ffmcap" / "score_weighted" / "ffmcap_tilt"
    # tilt_gamma=0.5,          # only for ffmcap_tilt
    # score_threshold=0.0,     # only for score_weighted

    # ----- Signal -----
    # signal_fn=my_signal,
    # signal_name="my_blend",
    # signal_top_n=None,        # keep top-N by z-score
    # signal_top_quantile=0.3,  # OR keep top quantile
    # winsor_sigma=3.0,

    # ----- Caps -----
    # max_stock_wt=0.20,
    # max_sector_wt=0.25,             # per-sector cap at `sector_level`
    # sector_level="sector",          # macro_sector | sector | industry | basic_industry
    # sector_map_csv=None,            # None = pipeline/data/sector_classification.csv
    # max_size_bucket_wt=None,        # e.g. {"small": 0.20}
    # size_buckets={"large": (1, 100), "mid": (101, 250), "small": (251, None)},

    # ----- Universe filters (pre-signal) -----
    # min_price=1.0,
    # min_adv_inr=None,           # absolute INR-turnover floor
    # min_history_days=0,         # exclude recent IPOs by setting >0
    # max_universe_rank=None,     # e.g. 200 = top 200 only
    # min_free_float_pct=None,
    # exclude_symbols=[],
    # extra_filters=[],           # user callables (pool, date, ctx) -> pool

    # ----- T-cost -----
    # tcost=TcostConfig(base=5.0, k_illiq=8.0, floor_bps=3.0, ceiling_bps=50.0),

    # ----- Cash buffer -----
    # cash_buffer=0.0,

    # ----- Benchmark -----
    # benchmark="nifty500_tri",   # or "nifty500_momentum50_tri"
    # benchmark_csv=None,         # None = cached CSV for `benchmark`

    # ----- Reporting -----
    # rolling_window_days=252,

    # ----- Identification -----
    # run_id=None,                # auto-generated if None
    # out_dir=None,               # None = backtest/data/backtests/<run_id>/
)

# Optional: auto-open dashboard in browser after the run
auto_open = False
