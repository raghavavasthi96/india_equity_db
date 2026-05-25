from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def _asof_series(panel: pd.DataFrame, date: pd.Timestamp, symbols) -> pd.Series:
    valid = panel.index[panel.index <= date]
    if len(valid) == 0:
        return pd.Series(np.nan, index=symbols)
    row = panel.loc[valid[-1]]
    cols = [s for s in symbols if s in row.index]
    out = pd.Series(np.nan, index=symbols)
    out.loc[cols] = row[cols].values
    return out


def rank_filter(pool: pd.DataFrame, date, ctx) -> pd.DataFrame:
    cap = ctx.cfg.max_universe_rank
    if cap is None:
        return pool
    return pool[pool["rank"] <= cap]


def price_filter(pool: pd.DataFrame, date, ctx) -> pd.DataFrame:
    floor = ctx.cfg.min_price
    if floor is None or floor <= 0:
        return pool
    px = _asof_series(ctx.close, date, pool.index)
    keep = px[px >= floor].index
    return pool.loc[pool.index.intersection(keep)]


def liquidity_filter(pool: pd.DataFrame, date, ctx) -> pd.DataFrame:
    floor = ctx.cfg.min_adv_inr
    if floor is None:
        return pool
    adv = _asof_series(ctx.adv_30d_inr, date, pool.index)
    keep = adv[adv >= floor].index
    return pool.loc[pool.index.intersection(keep)]


def history_filter(pool: pd.DataFrame, date, ctx) -> pd.DataFrame:
    n = ctx.cfg.min_history_days
    if n is None or n <= 0:
        return pool
    cutoff_idx = ctx.close.index <= date
    sub = ctx.close.loc[cutoff_idx, ctx.close.columns.intersection(pool.index)]
    counts = sub.notna().sum(axis=0)
    keep = counts[counts >= n].index
    return pool.loc[pool.index.intersection(keep)]


def free_float_filter(pool: pd.DataFrame, date, ctx) -> pd.DataFrame:
    floor = ctx.cfg.min_free_float_pct
    if floor is None:
        return pool
    ff_pct = pool["free_float_shares"] / pool["shares_outstanding"]
    keep = ff_pct[ff_pct >= floor].index
    return pool.loc[pool.index.intersection(keep)]


def blacklist_filter(pool: pd.DataFrame, date, ctx) -> pd.DataFrame:
    excl = ctx.cfg.exclude_symbols or []
    if not excl:
        return pool
    return pool.loc[~pool.index.isin(excl)]


PRE_SIGNAL_FILTERS = [
    rank_filter,
    blacklist_filter,
    free_float_filter,
    history_filter,
    price_filter,
    liquidity_filter,
]


def apply_universe_filters(pool: pd.DataFrame, date, ctx) -> pd.DataFrame:
    for f in PRE_SIGNAL_FILTERS:
        pool = f(pool, date, ctx)
        if len(pool) == 0:
            return pool
    for f in ctx.cfg.extra_filters or []:
        pool = f(pool, date, ctx)
        if len(pool) == 0:
            return pool
    return pool


def apply_post_signal_select(
    pool: pd.DataFrame, score: Optional[pd.Series], cfg
) -> pd.DataFrame:
    if score is None:
        return pool
    score = score.reindex(pool.index).dropna()
    if cfg.signal_top_n is not None:
        keep = score.nlargest(int(cfg.signal_top_n)).index
        return pool.loc[pool.index.intersection(keep)]
    if cfg.signal_top_quantile is not None:
        q = float(cfg.signal_top_quantile)
        thr = score.quantile(1.0 - q)
        keep = score[score >= thr].index
        return pool.loc[pool.index.intersection(keep)]
    return pool
