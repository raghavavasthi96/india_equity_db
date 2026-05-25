"""12-1m momentum, ff-mcap tilted, top-30% selected, quarterly rebalanced.

Run:
    python -m backtest.run_backtest backtest/configs/momentum_q.py
"""

from backtest import BacktestConfig, momentum_12_1

cfg = BacktestConfig(
    weighting="ffmcap_tilt",
    tilt_gamma=1.5,
    rebal_freq="M",
    signal_fn=momentum_12_1,
    signal_name="momentum_12_1",
    signal_top_n = 50,
    signal_top_quantile=None,
    max_stock_wt=0.10,
    run_id="momentum_m",
)

auto_open = False
