from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def standardise(raw: pd.Series, winsor_sigma: float = 3.0) -> pd.Series:
    raw = raw.dropna()
    if len(raw) == 0:
        return raw
    sd = raw.std()
    if sd == 0 or pd.isna(sd):
        return pd.Series(0.0, index=raw.index)
    z = (raw - raw.mean()) / sd
    z = z.clip(-winsor_sigma, winsor_sigma)
    sd2 = z.std()
    if sd2 == 0 or pd.isna(sd2):
        return pd.Series(0.0, index=raw.index)
    return (z - z.mean()) / sd2


# ---------------------------------------------------------------------------
# Stub signals — pure functions, signature (pool_index, date, ctx) -> Series
# ---------------------------------------------------------------------------

def momentum_12_1(pool, date, ctx) -> pd.Series:
    """Trailing 12m total return, skip last 21 trading days (~252d / 21d)."""
    close = ctx.close.loc[:date]
    syms = [s for s in pool if s in close.columns]
    if len(close) < 252 or not syms:
        return pd.Series(dtype=float)
    px = close[syms].iloc[-252:-21]
    if len(px) < 2:
        return pd.Series(dtype=float)
    return ((px.iloc[-1] / px.iloc[0]) - 1.0).dropna()


def vol_inverse(pool, date, ctx) -> pd.Series:
    """Inverse of trailing 1y realised vol on daily returns (~252 trading days)."""
    rets = ctx.returns.loc[:date]
    syms = [s for s in pool if s in rets.columns]
    if len(rets) < 60 or not syms:
        return pd.Series(dtype=float)
    sub = rets[syms].iloc[-252:]
    vol = sub.std()
    vol = vol[vol > 0]
    return 1.0 / vol


def earnings_growth_yoy(pool, date, ctx) -> pd.Series:
    """YoY growth of trailing net_profit using financials_panel.

    `financials_panel.date` is the quarter-end (Mar/Jun/Sep/Dec 31), NOT the
    filing date. SEBI gives Indian listed companies 45 days (quarterly) /
    60 days (annual) to file. We lag by `reporting_lag_days` (default 60) so
    period-end results are only used after they could plausibly be public.
    """
    fin = ctx.financials
    lag_days = getattr(ctx.cfg, "reporting_lag_days", 60)
    available_as_of = date - pd.Timedelta(days=lag_days)
    fin = fin[(fin["parameter"] == "net_profit") & (fin["date"] <= available_as_of)]
    if fin.empty:
        return pd.Series(dtype=float)
    last = fin.sort_values("date").groupby("symbol").tail(1).set_index("symbol")["value"]
    prior_cutoff = available_as_of - pd.Timedelta(days=365)
    fin_prior = fin[fin["date"] <= prior_cutoff]
    prior = (
        fin_prior.sort_values("date").groupby("symbol").tail(1).set_index("symbol")["value"]
    )
    common = last.index.intersection(prior.index).intersection(pool)
    if len(common) == 0:
        return pd.Series(dtype=float)
    last = last.loc[common]
    prior = prior.loc[common].replace(0, np.nan)
    growth = (last - prior) / prior.abs()
    return growth.dropna()


def compose(*signals, name: Optional[str] = None):
    """Build a composite signal from one or more sub-signals.

    Each item is either a bare signal function (weight = 1.0) or a
    `(signal_fn, weight)` tuple. Negative weights are allowed (invert).

    Pipeline applied at evaluation time:
      1. Call each sub-signal with the same (pool, date, ctx).
      2. Standardise each sub-signal cross-sectionally (winsorise → z-score)
         using `ctx.cfg.winsor_sigma`, so different units don't dominate.
      3. Weighted-sum on the union of returned symbols. A sub-signal that
         doesn't score a symbol contributes 0 for that symbol (neutral).
      4. Return the raw weighted sum — the engine's downstream
         `compute_signal()` standardises it again before weighting.

    Usage:
        compose(momentum_12_1, vol_inverse)                   # equal weight
        compose((momentum_12_1, 0.6), (vol_inverse, 0.4))     # weighted
        compose((momentum_12_1, 1.0), (vol_inverse, -1.0))    # long mom, short LV
    """
    pairs: list[tuple] = []
    for s in signals:
        if callable(s):
            pairs.append((s, 1.0))
        else:
            fn, w = s
            pairs.append((fn, float(w)))

    if not pairs:
        raise ValueError("compose() requires at least one sub-signal")

    def _composite(pool, date, ctx) -> pd.Series:
        ws = ctx.cfg.winsor_sigma
        components: list[tuple] = []
        for fn, w in pairs:
            raw = fn(list(pool), date, ctx)
            if raw is None or len(raw) == 0:
                continue
            components.append((standardise(raw, winsor_sigma=ws), w))
        if not components:
            return pd.Series(dtype=float)
        pool_idx = pd.Index(pool)
        idx = components[0][0].index
        for z, _ in components[1:]:
            idx = idx.union(z.index)
        idx = idx.intersection(pool_idx)
        combined = pd.Series(0.0, index=idx)
        for z, w in components:
            combined = combined + w * z.reindex(idx).fillna(0.0)
        return combined

    _composite.__name__ = name or (
        "compose("
        + "+".join(f"{w:g}*{fn.__name__}" for fn, w in pairs)
        + ")"
    )
    return _composite


# ---------------------------------------------------------------------------
# Pre-defined composite — kept minimal as a working example. Build your own
# via signals.compose(...) and register them here for CLI access.
# ---------------------------------------------------------------------------
mom_lvol = compose((momentum_12_1, 0.5), (vol_inverse, 0.5), name="mom_lvol")


SIGNAL_REGISTRY = {
    "momentum_12_1": momentum_12_1,
    "vol_inverse": vol_inverse,
    "earnings_growth_yoy": earnings_growth_yoy,
    "mom_lvol": mom_lvol,
}


def compute_signal(pool, date, ctx) -> Optional[pd.Series]:
    fn = ctx.cfg.signal_fn
    if fn is None:
        return None
    raw = fn(list(pool), date, ctx)
    if raw is None or len(raw) == 0:
        return None
    return standardise(raw, ctx.cfg.winsor_sigma)
