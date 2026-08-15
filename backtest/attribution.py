"""
attribution.py — Sector-level weight, return and cost attribution.

Every function here is pure: it takes finished engine output and reshapes it.
Nothing re-runs the backtest, so attribution can be recomputed from the
persisted CSVs of an old run.

Grouping uses `cfg.sector_level` of the NSE/BSE unified taxonomy, resolved into
`ctx.sector_map` at context build time.

Identities that hold by construction (checked in `verify_identities`):
  - sector weights sum to 1 each day (CASH is its own bucket)
  - sector gross contributions sum to the daily gross portfolio return
  - sector t-cost allocations sum to the per-rebalance t-cost drag
  - active weights sum to ~0 (up to the cash buffer)
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .context import CASH, benchmark_weights_asof

UNCLASSIFIED = "UNCLASSIFIED"


# ---------------------------------------------------------------------------
# Grouping helpers
# ---------------------------------------------------------------------------

def _sector_of(symbol: str, sector_map: dict) -> str:
    if symbol == CASH:
        return CASH
    return sector_map.get(symbol, UNCLASSIFIED)


def _group_columns(columns, sector_map: dict) -> dict:
    """sector -> list of columns belonging to it."""
    groups: dict = {}
    for col in columns:
        groups.setdefault(_sector_of(col, sector_map), []).append(col)
    return groups


def _collapse(wide: pd.DataFrame, sector_map: dict) -> pd.DataFrame:
    """Sum a date x symbol panel into a date x sector panel."""
    groups = _group_columns(wide.columns, sector_map)
    out = pd.DataFrame(
        {sector: wide[cols].sum(axis=1) for sector, cols in groups.items()},
        index=wide.index,
    )
    out.index.name = "date"
    return out.sort_index(axis=1)


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

def sector_weights(weights_df: pd.DataFrame, sector_map: dict) -> pd.DataFrame:
    """Daily date x sector weight panel. Rows sum to 1 (CASH included)."""
    if weights_df.empty or not sector_map:
        return pd.DataFrame()
    return _collapse(weights_df, sector_map)


# ---------------------------------------------------------------------------
# Return / cost attribution
# ---------------------------------------------------------------------------

def _earning_weights(weights_df: pd.DataFrame,
                     diagnostics: Optional[pd.DataFrame]) -> pd.DataFrame:
    """
    The weight vector that actually earned each day's return.

    The engine appends *post-drift* weights to the daily panel, so on an ordinary
    day the earning weights are simply the previous row. On a trade date it
    rebalances to the target *before* accruing that day's return, so the earning
    weights are the post-cap target — which the engine already recorded per name
    in `rebalance_diagnostics.post_cap_weight`.
    """
    earning = weights_df.shift(1).fillna(0.0)
    if diagnostics is None or diagnostics.empty:
        return earning

    diag = diagnostics.copy()
    diag["trade_date"] = pd.to_datetime(diag["trade_date"])
    targets = diag.pivot_table(index="trade_date", columns="symbol",
                               values="post_cap_weight", aggfunc="last")
    for trade_date, row in targets.iterrows():
        if trade_date not in earning.index:
            continue
        earning.loc[trade_date, :] = 0.0
        row = row.dropna()
        cols = [c for c in row.index if c in earning.columns]
        earning.loc[trade_date, cols] = row[cols].values
    return earning


def _sector_tcost(dates, sectors, sector_map: dict,
                  diagnostics: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Per-name t-cost from the diagnostics blotter, summed into sectors by trade date."""
    out = pd.DataFrame(0.0, index=dates, columns=sectors)
    if diagnostics is None or diagnostics.empty:
        return out
    diag = diagnostics.copy()
    diag["trade_date"] = pd.to_datetime(diag["trade_date"])
    diag["cost"] = diag["delta_weight"].abs() * diag["cost_bps"].fillna(0.0) / 10000.0
    diag["sector"] = [_sector_of(s, sector_map) for s in diag["symbol"]]
    grouped = diag.groupby(["trade_date", "sector"])["cost"].sum().unstack(fill_value=0.0)
    return grouped.reindex(index=dates, columns=sectors).fillna(0.0)


def sector_contribution(
    weights_df: pd.DataFrame,
    asset_returns: pd.DataFrame,
    portfolio_returns: pd.DataFrame,
    sector_map: dict,
    diagnostics: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Long-format `date, sector, weight, contrib_gross, contrib_tcost, contrib_net,
    cum_contrib_net_pct`.

    `asset_returns` is `ctx.returns` (date x symbol); `portfolio_returns` is the
    engine's `returns` frame (gross / net / benchmark / active).

    Gross contribution is `w_earning[t] * r[t]` summed within a sector — the same
    arithmetic the engine uses to accrue the portfolio return, so sector
    contributions add back to `gross` exactly, with no linking residual.

    T-cost is allocated from the per-name costs the engine already recorded, so
    net attribution is exact rather than approximated.

    Cumulative contribution is accumulated in NAV units (`nav[t-1] * contrib[t]`)
    and reported as a percentage of starting NAV. Summing daily percentage
    contributions would not reconcile to the compounded total return; this does,
    and it needs no Cariño-style linking coefficient.
    """
    if weights_df.empty or not sector_map:
        return pd.DataFrame()

    dates = weights_df.index
    investable = [c for c in weights_df.columns if c != CASH]

    earning = _earning_weights(weights_df, diagnostics)[investable]
    rets = asset_returns.reindex(index=dates, columns=investable).fillna(0.0)

    gross = _collapse(earning * rets, sector_map)
    # CASH holds weight but earns nothing; keep the bucket so the sector set
    # matches sector_weights().
    if CASH not in gross.columns:
        gross[CASH] = 0.0
    gross = gross.sort_index(axis=1)

    tcost = _sector_tcost(dates, gross.columns, sector_map, diagnostics)
    # The engine applies t-cost as a NAV haircut, net = (1+gross)(1-drag)-1, not
    # gross-drag. Mirroring the shrink here keeps the sector net contributions
    # summing to the engine's net return exactly rather than to within the
    # gross x drag cross-term.
    drag = tcost.sum(axis=1)
    net = gross.mul(1.0 - drag, axis=0) - tcost
    weights = sector_weights(weights_df, sector_map).reindex(
        index=dates, columns=gross.columns).fillna(0.0)

    nav_prev = (1.0 + portfolio_returns["net"].reindex(dates).fillna(0.0)).cumprod().shift(1).fillna(1.0)
    cum_net_pct = net.mul(nav_prev, axis=0).cumsum() * 100.0

    parts = {
        "weight": weights,
        "contrib_gross": gross,
        "contrib_tcost": tcost,
        "contrib_net": net,
        "cum_contrib_net_pct": cum_net_pct,
    }
    long = None
    for name, frame in parts.items():
        melted = (frame.rename_axis(index="date", columns="sector")
                       .stack().rename(name).reset_index())
        long = melted if long is None else long.merge(melted, on=["date", "sector"])
    return long.sort_values(["date", "sector"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Active weights vs the ff-mcap benchmark
# ---------------------------------------------------------------------------

def sector_active_weights(weights_df: pd.DataFrame, ctx, sector_map: dict) -> pd.DataFrame:
    """
    Long-format `date, sector, portfolio_wt, benchmark_wt, active_wt`.

    The benchmark is the PIT free-float-mcap universe (`ffmcap_top500`), NOT the
    Nifty 500 — the TRI file carries index levels only, never constituents, so
    true index weights are unavailable. Label any chart built on this accordingly.
    """
    if weights_df.empty or not sector_map:
        return pd.DataFrame()

    port = sector_weights(weights_df, sector_map)

    # Benchmark weights only move at quarterly snapshots: collapse once per
    # snapshot, then broadcast to the days that snapshot governs.
    snap_dates = ctx.rebal_quarter_ends
    bench_by_snap: dict = {}
    for qed in snap_dates:
        bw = benchmark_weights_asof(ctx, qed)
        if bw.empty:
            continue
        by_sector: dict = {}
        for sym, w in bw.items():
            sector = _sector_of(sym, sector_map)
            by_sector[sector] = by_sector.get(sector, 0.0) + float(w)
        bench_by_snap[qed] = by_sector

    snap_for_day = {}
    for date in weights_df.index:
        valid = snap_dates[snap_dates <= date]
        snap_for_day[date] = valid[-1] if len(valid) else None

    rows = []
    for date in weights_df.index:
        bench = bench_by_snap.get(snap_for_day[date], {})
        for sector in set(port.columns) | set(bench):
            p = float(port.at[date, sector]) if sector in port.columns else 0.0
            b = float(bench.get(sector, 0.0))
            rows.append({"date": date, "sector": sector,
                         "portfolio_wt": p, "benchmark_wt": b, "active_wt": p - b})

    return pd.DataFrame(rows).sort_values(["date", "sector"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_sector_frames(result) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    `(attribution, active)` for a finished run, folding the headline numbers into
    `result.summary`. Memoised on the result so the writer and the dashboard
    share one computation.

    Both frames come back empty when no sector map is configured, so a run
    without `sector_classification.csv` degrades exactly as it did before.
    """
    cached = getattr(result, "_sector_frames", None)
    if cached is not None:
        return cached

    sector_map = result.ctx.sector_map
    if not sector_map:
        frames = (pd.DataFrame(), pd.DataFrame())
        result._sector_frames = frames
        return frames

    attribution = sector_contribution(
        result.weights, result.ctx.returns, result.returns,
        sector_map, result.diagnostics,
    )
    active = sector_active_weights(result.weights, result.ctx, sector_map)

    if not attribution.empty:
        end = attribution["date"].max()
        result.summary["sector_level"] = result.cfg.sector_level
        result.summary["sector_contribution_net"] = (
            attribution[attribution["date"] == end]
            .set_index("sector")["cum_contrib_net_pct"].round(4).to_dict()
        )
        result.summary["sector_attribution_residuals"] = verify_identities(
            result, attribution, active)
    if not active.empty:
        end = active["date"].max()
        result.summary["sector_active_wt_end"] = (
            active[active["date"] == end]
            .set_index("sector")["active_wt"].round(6).to_dict()
        )

    frames = (attribution, active)
    result._sector_frames = frames
    return frames


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

def verify_identities(result, attribution: pd.DataFrame,
                      active: pd.DataFrame, tol: float = 1e-9) -> dict:
    """
    Reconcile the attribution against the engine's own numbers. Returns a dict of
    max absolute deviations — all should be at floating-point noise.
    """
    out = {}
    if attribution.empty:
        return out

    by_date = attribution.groupby("date")
    out["weight_sum_vs_1"] = float((by_date["weight"].sum() - 1.0).abs().max())

    gross = by_date["contrib_gross"].sum()
    out["gross_vs_engine"] = float(
        (gross - result.returns["gross"].reindex(gross.index)).abs().max())

    net = by_date["contrib_net"].sum()
    out["net_vs_engine"] = float(
        (net - result.returns["net"].reindex(net.index)).abs().max())

    tcost_total = attribution["contrib_tcost"].sum()
    engine_tcost = result.tcost["drag_decimal"].sum() if not result.tcost.empty else 0.0
    out["tcost_vs_engine"] = float(abs(tcost_total - engine_tcost))

    # Portfolio weights (incl. CASH) and benchmark weights each sum to 1, so the
    # active column must sum to 0 on every day regardless of the cash buffer.
    if not active.empty:
        out["active_sum_vs_0"] = float(
            active.groupby("date")["active_wt"].sum().abs().max())
    return out
