"""12-1m momentum, ff-mcap tilted, top-30% selected, quarterly rebalanced.

Run:
    python -m backtest.run_backtest backtest/configs/momentum_q.py
"""

from backtest import BacktestConfig, vol_inverse

cfg = BacktestConfig(
    weighting="ffmcap_tilt",
    tilt_gamma=1.5,
    rebal_freq="Q",
    signal_fn=vol_inverse,
    signal_name="lvol",
    signal_top_n = 50,
    signal_top_quantile=None,
    max_stock_wt=0.10,
    run_id="lvol_q",
)

auto_open = False
