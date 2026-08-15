from __future__ import annotations

from typing import Optional, Dict, Tuple

import numpy as np
import pandas as pd

from .context import CASH


def _normalise(w: pd.Series) -> pd.Series:
    s = w.sum()
    if s <= 0 or not np.isfinite(s):
        return pd.Series(dtype=float)
    return w / s


def weight_equal(pool: pd.DataFrame, score, snap, cfg) -> pd.Series:
    n = len(pool)
    if n == 0:
        return pd.Series(dtype=float)
    return pd.Series(1.0 / n, index=pool.index)


def weight_ffmcap(pool: pd.DataFrame, score, snap, cfg) -> pd.Series:
    w = pool["free_float_market_cap_inr"].astype(float)
    w = w[w > 0]
    return _normalise(w)


def weight_score(pool: pd.DataFrame, score, snap, cfg) -> pd.Series:
    if score is None:
        raise ValueError("score_weighted requires signal_fn in BacktestConfig")
    z = score.reindex(pool.index).dropna()
    pos = (z - cfg.score_threshold).clip(lower=0)
    pos = pos[pos > 0]
    if len(pos) == 0:
        raise ValueError(
            f"score_weighted produced empty weights at threshold={cfg.score_threshold}"
        )
    return _normalise(pos)


def weight_ffmcap_tilt(pool: pd.DataFrame, score, snap, cfg) -> pd.Series:
    if score is None:
        raise ValueError("ffmcap_tilt requires signal_fn in BacktestConfig")
    z = score.reindex(pool.index).fillna(0.0)
    ff = pool["free_float_market_cap_inr"].astype(float).reindex(pool.index)
    ff = ff.where(ff > 0, 0.0)
    tilt = np.exp(cfg.tilt_gamma * z)
    w = ff * tilt
    return _normalise(w[w > 0])


WEIGHTING_FNS = {
    "equal": weight_equal,
    "ffmcap": weight_ffmcap,
    "score_weighted": weight_score,
    "ffmcap_tilt": weight_ffmcap_tilt,
}


def compute_target_weights(pool, score, snap, cfg) -> pd.Series:
    fn = WEIGHTING_FNS.get(cfg.weighting)
    if fn is None:
        raise ValueError(f"Unknown weighting mode: {cfg.weighting}")
    return fn(pool, score, snap, cfg)


# ---------------------------------------------------------------------------
# Cap solver
# ---------------------------------------------------------------------------

def _waterfill_single_cap(w: pd.Series, group_cap: float, groups: pd.Series) -> pd.Series:
    """Apply a single cap to groups (group key per symbol). For per-stock cap pass
    groups = Series(index=w.index, values=w.index)."""
    if group_cap is None or group_cap <= 0:
        return w
    w = w.copy()
    for _ in range(200):
        agg = w.groupby(groups).sum()
        over = agg[agg > group_cap + 1e-12]
        if len(over) == 0:
            return w
        for g, total in over.items():
            members = groups[groups == g].index
            members = members.intersection(w.index)
            if len(members) == 0:
                continue
            scale = group_cap / total
            w.loc[members] = w.loc[members] * scale
        # redistribute residual to uncapped uniformly to keep sum = 1
        residual = 1.0 - w.sum()
        if abs(residual) < 1e-12:
            continue
        uncapped_groups = agg[agg <= group_cap - 1e-12].index
        uncapped = groups[groups.isin(uncapped_groups)].index.intersection(w.index)
        if len(uncapped) == 0:
            return w  # cannot redistribute — infeasible-ish, leave as is
        add = residual * (w.loc[uncapped] / w.loc[uncapped].sum())
        w.loc[uncapped] = w.loc[uncapped] + add
    return w


def _size_bucket_groups(pool: pd.DataFrame, size_buckets: dict) -> pd.Series:
    rank = pool["rank"].astype(int)
    out = pd.Series(index=pool.index, dtype=object)
    for name, (lo, hi) in size_buckets.items():
        hi_val = hi if hi is not None else 10**9
        mask = (rank >= lo) & (rank <= hi_val)
        out.loc[mask[mask].index] = name
    out = out.fillna("other")
    return out


def apply_caps(
    w: pd.Series,
    pool: pd.DataFrame,
    cfg,
    sector_map: Optional[dict],
) -> pd.Series:
    if len(w) == 0:
        return w
    if cfg.max_stock_wt and cfg.max_stock_wt * len(w) < 1.0:
        raise ValueError(
            f"Infeasible: max_stock_wt={cfg.max_stock_wt} x N={len(w)} = "
            f"{cfg.max_stock_wt*len(w):.3f} < 1.0"
        )

    stock_groups = pd.Series(w.index, index=w.index)
    sector_groups = None
    if cfg.max_sector_wt is not None:
        if sector_map is None:
            import logging
            logging.warning(
                "max_sector_wt set but sector_map_csv missing/empty — sector cap disabled"
            )
        else:
            sector_groups = pd.Series(
                {s: sector_map.get(s, "UNKNOWN") for s in w.index}
            )
            n_sectors = sector_groups.nunique()
            if cfg.max_sector_wt * n_sectors < 1.0:
                raise ValueError(
                    f"Infeasible: max_sector_wt={cfg.max_sector_wt} x "
                    f"{n_sectors} sectors = {cfg.max_sector_wt*n_sectors:.3f} < 1.0"
                )

    size_groups = None
    if cfg.max_size_bucket_wt:
        size_groups = _size_bucket_groups(pool.loc[w.index], cfg.size_buckets)

    for outer in range(20):
        w_before = w.copy()
        if cfg.max_stock_wt:
            w = _waterfill_single_cap(w, cfg.max_stock_wt, stock_groups)
        if sector_groups is not None:
            w = _waterfill_single_cap(w, cfg.max_sector_wt, sector_groups)
        if size_groups is not None:
            # buckets may have different caps; apply each one in turn
            for bucket_name, bucket_cap in cfg.max_size_bucket_wt.items():
                bucket_mask = (size_groups == bucket_name)
                if not bucket_mask.any():
                    continue
                phantom_groups = pd.Series(
                    np.where(bucket_mask, bucket_name, "_other"),
                    index=size_groups.index,
                )
                w = _waterfill_single_cap(w, bucket_cap, phantom_groups)
        if np.allclose(w.values, w_before.reindex(w.index).fillna(0).values, atol=1e-10):
            return w
    raise RuntimeError("Cap solver did not converge in 20 outer iterations")


def apply_cash_buffer(w: pd.Series, cfg) -> pd.Series:
    if cfg.cash_buffer <= 0:
        return w
    w = w * (1.0 - cfg.cash_buffer)
    w[CASH] = w.get(CASH, 0.0) + cfg.cash_buffer
    return w
