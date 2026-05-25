from __future__ import annotations

import numpy as np
import pandas as pd

from .signals import standardise


def _asof_value(panel: pd.DataFrame, date: pd.Timestamp, symbols) -> pd.Series:
    valid = panel.index[panel.index <= date]
    if len(valid) == 0:
        return pd.Series(np.nan, index=symbols)
    row = panel.loc[valid[-1]]
    cols = [s for s in symbols if s in row.index]
    out = pd.Series(np.nan, index=symbols, dtype=float)
    out.loc[cols] = row[cols].astype(float).values
    return out


def cost_bps_per_name(symbols, trade_date, ctx) -> pd.Series:
    """Composite illiquidity z-score → cost bps. Computed cross-sectionally over
    the eligible pool at trade_date."""
    symbols = [s for s in symbols if s != "CASH"]
    if not symbols:
        return pd.Series(dtype=float)

    snap = ctx.snap_asof(trade_date)
    mcap = snap.reindex(symbols)["market_cap_inr"].astype(float)
    ff = snap.reindex(symbols)["free_float_shares"].astype(float) / snap.reindex(
        symbols
    )["shares_outstanding"].astype(float)
    adv = _asof_value(ctx.adv_30d_inr, trade_date, symbols)

    # Pieces — negate so high illiquidity gives positive z
    def _z(s, transform=None):
        s2 = s.copy()
        if transform == "log":
            s2 = np.log10(s2.where(s2 > 0))
        return standardise(s2, winsor_sigma=5.0)

    z_mcap = _z(-np.log10(mcap.where(mcap > 0)), None)
    z_adv = _z(-np.log10(adv.where(adv > 0)), None)
    z_ff = _z(-ff, None)

    illiq_raw = (
        z_mcap.reindex(symbols).fillna(0)
        + z_adv.reindex(symbols).fillna(0)
        + z_ff.reindex(symbols).fillna(0)
    )
    illiq_z = standardise(illiq_raw, winsor_sigma=5.0).reindex(symbols).fillna(0)

    tc = ctx.cfg.tcost
    cost = tc.base + tc.k_illiq * illiq_z.clip(lower=0)
    cost = cost.clip(lower=tc.floor_bps, upper=tc.ceiling_bps)
    return cost


def tcost_drag(deltas: pd.Series, trade_date, ctx) -> tuple[float, pd.Series]:
    """Return (total_drag_decimal, per_name_cost_bps_series).
    deltas = w_target - w_drifted, indexed by symbol (incl. CASH; CASH cost = 0).
    """
    deltas = deltas[deltas.index != "CASH"]
    abs_delta = deltas.abs()
    abs_delta = abs_delta[abs_delta > 1e-9]
    if len(abs_delta) == 0:
        return 0.0, pd.Series(dtype=float)
    cost_bps = cost_bps_per_name(list(abs_delta.index), trade_date, ctx)
    cost_bps = cost_bps.reindex(abs_delta.index).fillna(ctx.cfg.tcost.base)
    drag = float((abs_delta * cost_bps).sum() / 10000.0)
    return drag, cost_bps
