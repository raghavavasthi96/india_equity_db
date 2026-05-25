from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Dict

import numpy as np
import pandas as pd

from .configs.config import BacktestConfig
from .context import (
    BacktestContext,
    build_context,
    build_rebal_schedule,
    trade_date_for,
    CASH,
)
from .universe import apply_universe_filters, apply_post_signal_select
from .signals import compute_signal
from .weights import compute_target_weights, apply_caps, apply_cash_buffer
from .tcost import tcost_drag


@dataclass
class BacktestResult:
    cfg: BacktestConfig
    weights: pd.DataFrame              # daily date x symbol
    returns: pd.DataFrame              # gross / net / bench / active (date)
    diagnostics: pd.DataFrame          # per rebal_date x symbol: raw/post_cap/drift/delta/cost_bps
    turnover: pd.DataFrame             # per rebal_date
    tcost: pd.DataFrame                # per rebal_date
    summary: dict
    ctx: BacktestContext = field(repr=False)


def _drift_weights(prev_w: pd.Series, day_ret: pd.Series) -> tuple[pd.Series, float]:
    """Buy-and-hold drift: w[t] = w[t-1]*(1+r[t]) / (1 + port_r)."""
    aligned_ret = day_ret.reindex(prev_w.index).fillna(0.0)
    # CASH return = 0
    if CASH in aligned_ret.index:
        aligned_ret[CASH] = 0.0
    port_r = float((prev_w * aligned_ret).sum())
    new_w = prev_w * (1.0 + aligned_ret)
    denom = 1.0 + port_r
    if denom <= 0:
        return new_w, port_r
    new_w = new_w / denom
    return new_w, port_r


def _liquidate_delisted(w: pd.Series, date: pd.Timestamp, ctx: BacktestContext) -> pd.Series:
    """Move weight of any symbol whose last_valid_date < `date` into CASH."""
    if len(w) == 0:
        return w
    syms = [s for s in w.index if s != CASH]
    if not syms:
        return w
    lv = ctx.last_valid_date
    dead = [s for s in syms if (s not in lv.index) or (lv[s] < date)]
    if not dead:
        return w
    transferred = float(w.loc[dead].sum())
    w = w.drop(index=dead)
    w[CASH] = w.get(CASH, 0.0) + transferred
    return w


def _build_target_for_date(
    rebal_date: pd.Timestamp, ctx: BacktestContext, cfg: BacktestConfig
) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    """Returns (w_target_with_cash, w_raw, snap_used). Pure — no I/O."""
    snap = ctx.snap_asof(rebal_date)
    if snap.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), snap
    pool = snap.copy()
    pool = apply_universe_filters(pool, rebal_date, ctx)
    if len(pool) == 0:
        return pd.Series(dtype=float), pd.Series(dtype=float), snap
    score = compute_signal(list(pool.index), rebal_date, ctx)
    pool = apply_post_signal_select(pool, score, cfg)
    if len(pool) == 0:
        return pd.Series(dtype=float), pd.Series(dtype=float), snap
    w_raw = compute_target_weights(pool, score, snap, cfg)
    w_raw = w_raw[w_raw > 0]
    if len(w_raw) == 0:
        return pd.Series(dtype=float), pd.Series(dtype=float), snap
    w_capped = apply_caps(w_raw, pool, cfg, ctx.sector_map)
    w_final = apply_cash_buffer(w_capped, cfg)
    return w_final, w_raw, snap


def _load_benchmark(ctx: BacktestContext, span: pd.DatetimeIndex) -> pd.Series:
    """Load Nifty 500 TRI from CSV; align to backtest span as daily returns."""
    from .utils.benchmark import load_nifty500_tri, DEFAULT_PATH
    path = ctx.cfg.benchmark_csv or DEFAULT_PATH
    tri = load_nifty500_tri(path)
    tri = tri.reindex(span).ffill()
    rets = tri.pct_change().fillna(0.0)
    rets.name = "benchmark"
    rets.index.name = "date"
    return rets


def run_backtest(cfg: BacktestConfig, ctx: Optional[BacktestContext] = None) -> BacktestResult:
    if ctx is None:
        ctx = build_context(cfg)

    schedule = build_rebal_schedule(cfg, ctx)
    if len(schedule) == 0:
        raise ValueError("Empty rebalance schedule")

    start = schedule[0]
    end = pd.Timestamp(cfg.end) if cfg.end else ctx.trading_dates[-1]
    span = ctx.trading_dates[(ctx.trading_dates >= start) & (ctx.trading_dates <= end)]

    trade_dates = {rd: trade_date_for(rd, cfg.rebal_offset, ctx) for rd in schedule}
    trade_dates_set = {trade_dates[rd] for rd in schedule}

    # Pre-compute targets at each rebal date (uses PIT snapshot from rebal_date)
    targets: Dict[pd.Timestamp, pd.Series] = {}
    raw_by_date: Dict[pd.Timestamp, pd.Series] = {}
    for rd in schedule:
        w_final, w_raw, _ = _build_target_for_date(rd, ctx, cfg)
        if len(w_final) == 0:
            continue
        targets[trade_dates[rd]] = w_final
        raw_by_date[trade_dates[rd]] = w_raw

    # Daily loop
    w_curr = pd.Series({CASH: 1.0})
    weight_rows: list[tuple] = []
    gross_rets: list[tuple] = []
    net_rets: list[tuple] = []
    diag_rows: list[dict] = []
    turnover_rows: list[dict] = []
    tcost_rows: list[dict] = []

    nav = 1.0
    for d in span:
        # Handle delistings before trading / drift
        w_curr = _liquidate_delisted(w_curr, d, ctx)

        # Rebalance on trade date
        if d in trade_dates_set:
            tgt = targets[d].copy()
            # Ensure tgt has CASH key for clean alignment
            if CASH not in tgt.index:
                tgt[CASH] = 0.0
            all_syms = w_curr.index.union(tgt.index)
            w_before = w_curr.reindex(all_syms).fillna(0.0)
            w_after = tgt.reindex(all_syms).fillna(0.0)
            deltas = w_after - w_before
            drag, per_name_bps = tcost_drag(deltas, d, ctx)
            # Apply drag to NAV (proportional shrink)
            nav = nav * (1.0 - drag)
            # Find original rebal_date (for diagnostics keying)
            rd_match = next(rd for rd, td in trade_dates.items() if td == d)
            turnover_rows.append({
                "rebal_date": rd_match,
                "trade_date": d,
                "one_way_turnover": float(deltas.abs().sum() / 2.0),
            })
            tcost_rows.append({
                "rebal_date": rd_match,
                "trade_date": d,
                "drag_decimal": drag,
                "drag_bps": drag * 10000.0,
                "nav_inr_notional": nav,
            })
            # Diagnostics
            raw = raw_by_date.get(d, pd.Series(dtype=float))
            for s in all_syms:
                diag_rows.append({
                    "rebal_date": rd_match,
                    "trade_date": d,
                    "symbol": s,
                    "raw_weight": float(raw.get(s, np.nan)) if s in raw.index else np.nan,
                    "post_cap_weight": float(w_after.get(s, 0.0)),
                    "drift_weight": float(w_before.get(s, 0.0)),
                    "delta_weight": float(deltas.get(s, 0.0)),
                    "cost_bps": float(per_name_bps.get(s, np.nan))
                    if s in per_name_bps.index else np.nan,
                })
            w_curr = w_after.copy()
            # Mark net return on this day = gross - drag
            # Compute gross drift below as usual
        # Daily drift
        day_r = ctx.returns.loc[d] if d in ctx.returns.index else pd.Series(dtype=float)
        new_w, port_r = _drift_weights(w_curr, day_r)
        w_curr = new_w

        gross_rets.append((d, port_r))
        # Net return = gross return + (NAV-impact). NAV adjustment already happened
        # so accumulate via separate net path below.
        weight_rows.append((d, w_curr.copy()))

    # Build daily weights DataFrame
    all_syms = sorted({s for _, w in weight_rows for s in w.index})
    weights_df = pd.DataFrame(0.0, index=[d for d, _ in weight_rows], columns=all_syms)
    for d, w in weight_rows:
        weights_df.loc[d, w.index] = w.values
    weights_df.index.name = "date"

    gross_s = pd.Series(dict(gross_rets), name="gross")
    gross_s.index.name = "date"

    # Net = gross with t-cost drag applied on trade dates
    drag_s = pd.Series(0.0, index=gross_s.index)
    for r in tcost_rows:
        if r["trade_date"] in drag_s.index:
            drag_s.loc[r["trade_date"]] = r["drag_decimal"]
    net_s = (1.0 + gross_s) * (1.0 - drag_s) - 1.0
    net_s.name = "net"

    # Benchmark
    bench_s = _load_benchmark(ctx, gross_s.index).reindex(gross_s.index).fillna(0.0)
    active_s = (net_s - bench_s).rename("active")

    returns_df = pd.concat([gross_s, net_s, bench_s, active_s], axis=1)

    diag_df = pd.DataFrame(diag_rows)
    turn_df = pd.DataFrame(turnover_rows)
    tc_df = pd.DataFrame(tcost_rows)

    from .metrics import compute_summary
    summary = compute_summary(returns_df, weights_df, turn_df, tc_df, cfg)

    return BacktestResult(
        cfg=cfg,
        weights=weights_df,
        returns=returns_df,
        diagnostics=diag_df,
        turnover=turn_df,
        tcost=tc_df,
        summary=summary,
        ctx=ctx,
    )
