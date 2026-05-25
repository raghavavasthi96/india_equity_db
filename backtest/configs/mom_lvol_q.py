"""Composite momentum + low-volatility, ff-mcap tilted, top-30%, quarterly.

Demonstrates the `compose()` API for combining multiple sub-signals. Each
sub-signal is standardised cross-sectionally before being weighted-summed;
the engine re-standardises the composite output before weighting.

Run:
    python -m backtest.run_backtest backtest/configs/mom_lvol_q.py
"""

from backtest import BacktestConfig, compose, momentum_12_1, vol_inverse

mom_lvol = compose(
    (momentum_12_1, 0.5),
    (vol_inverse, 0.5),
    name="mom_lvol_50_50",
)

cfg = BacktestConfig(
    weighting="ffmcap_tilt",
    tilt_gamma=1.0,
    rebal_freq="Q",
    signal_fn=mom_lvol,
    signal_name="mom_lvol_50_50",
    signal_top_quantile=0.3,
    max_stock_wt=0.10,
    run_id="mom_lvol_q",
)

auto_open = False
