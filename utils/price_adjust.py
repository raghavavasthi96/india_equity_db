"""
utils/price_adjust.py — Back-adjust OHLCV for splits, bonuses, dividends.

Matches yfinance auto_adjust=True semantics. For each corp action on ex_date,
all bars *before* ex_date are multiplied by a cumulative factor.

Formulas:
  Split N:M (N old → M new, e.g. FV 10 → FV 2 means N=10 M=2 → 5-for-1):
      price_factor  = M / N
      volume_factor = N / M
  Bonus N:M (N new bonus shares per M held):
      price_factor  = M / (N + M)
      volume_factor = (N + M) / M
  Cash dividend D on close C_prev (close on trading day before ex_date):
      price_factor  = (C_prev - D) / C_prev
      volume_factor = 1.0

Inputs:
  prices_df: DataFrame indexed by date with columns [open, high, low, close, volume]
  actions_df: DataFrame with columns [ex_date, action_type, ratio_num, ratio_denom, amount]

Output:
  Same shape as prices_df with adjusted values. Original unchanged.
"""

import pandas as pd

from utils.logger import get_logger

logger = get_logger("price_adjust")

PRICE_COLS = ["open", "high", "low", "close"]


_MIN_PRICE_FACTOR = 0.01


def _action_factors(action: dict, close_prev: float, symbol: str = "") -> tuple:
    """Return (price_factor, volume_factor) for a single action, or (None, None) if invalid."""
    t = action["action_type"]
    if t == "split":
        n, m = action["ratio_num"], action["ratio_denom"]
        if not n or not m or m <= 0 or n <= m:
            return None, None
        return m / n, n / m
    if t == "bonus":
        n, m = action["ratio_num"], action["ratio_denom"]
        if not n or not m or n <= 0 or m <= 0:
            return None, None
        return m / (n + m), (n + m) / m
    if t == "dividend":
        d = action["amount"]
        if not d or d <= 0 or close_prev is None or close_prev <= 0:
            return None, None
        # Special / liquidation dividends can equal or exceed close_prev. Clamp instead
        # of dropping so the event still affects the back-adjustment chain.
        if d >= close_prev:
            logger.warning(
                f"{symbol}: dividend {d} >= close_prev {close_prev}; clamping price_factor to {_MIN_PRICE_FACTOR}"
            )
            return _MIN_PRICE_FACTOR, 1.0
        return (close_prev - d) / close_prev, 1.0
    return None, None


def apply_adjustments(prices_df: pd.DataFrame, actions_df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
    """
    Back-adjust prices_df for all events in actions_df.
    prices_df must be indexed by pd.Timestamp (date), sorted ascending.
    Returns a new DataFrame; input unchanged.
    """
    if prices_df.empty or actions_df is None or actions_df.empty:
        return prices_df.copy()

    df = prices_df.copy().sort_index()
    has_volume = "volume" in df.columns

    actions_sorted = actions_df.sort_values("ex_date", ascending=False).reset_index(drop=True)

    for _, action in actions_sorted.iterrows():
        ex_date = pd.Timestamp(action["ex_date"])
        prior_mask = df.index < ex_date
        if not prior_mask.any():
            continue

        close_prev = float(df.loc[prior_mask, "close"].iloc[-1]) if "close" in df.columns else None
        pf, vf = _action_factors(action.to_dict(), close_prev, symbol=symbol)
        if pf is None:
            logger.debug(f"{symbol}: skipped {action['action_type']} on {ex_date.date()} (invalid)")
            continue

        for col in PRICE_COLS:
            if col in df.columns:
                df.loc[prior_mask, col] = df.loc[prior_mask, col] * pf
        if has_volume:
            df.loc[prior_mask, "volume"] = df.loc[prior_mask, "volume"] * vf

    return df
