"""Default benchmark-like backtest: quarterly free-float mcap-weighted top-500.

No signal, 20% stock cap, no cash buffer. Closest to the underlying Nifty 500 TRI;
useful as a sanity baseline for comparing alpha strategies against.

Run:
    python -m backtest.run_backtest backtest/configs/default_ffmcap_q.py
"""

from backtest import BacktestConfig

cfg = BacktestConfig(
    weighting="ffmcap",
    rebal_freq="Q",
    max_stock_wt=0.20,
    run_id="default_ffmcap_q",
)

auto_open = False
