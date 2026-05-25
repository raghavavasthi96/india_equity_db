from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


TRADING_DAYS = 252


def _cumret(r: pd.Series) -> pd.Series:
    return (1.0 + r.fillna(0.0)).cumprod()


def cagr(r: pd.Series) -> float:
    if len(r) == 0:
        return 0.0
    eq = _cumret(r)
    years = len(r) / TRADING_DAYS
    if years <= 0 or eq.iloc[-1] <= 0:
        return 0.0
    return float(eq.iloc[-1] ** (1.0 / years) - 1.0)


def ann_vol(r: pd.Series) -> float:
    if len(r) < 2:
        return 0.0
    return float(r.std() * np.sqrt(TRADING_DAYS))


def sharpe(r: pd.Series) -> float:
    v = ann_vol(r)
    if v == 0:
        return 0.0
    return float(cagr(r) / v)


def sortino(r: pd.Series) -> float:
    if len(r) < 2:
        return 0.0
    downside = r[r < 0]
    if len(downside) == 0:
        return 0.0
    dd_vol = float(downside.std() * np.sqrt(TRADING_DAYS))
    if dd_vol == 0:
        return 0.0
    return float(cagr(r) / dd_vol)


def drawdown_series(r: pd.Series) -> pd.Series:
    eq = _cumret(r)
    return eq / eq.cummax() - 1.0


def max_drawdown(r: pd.Series) -> dict:
    dd = drawdown_series(r)
    if len(dd) == 0 or dd.min() == 0:
        return {"max_drawdown": 0.0, "start": None, "end": None, "recovery_days": None}
    end = dd.idxmin()
    start = (dd.loc[:end])[dd.loc[:end] == 0].index
    start = start[-1] if len(start) else dd.index[0]
    post = dd.loc[end:]
    recov = post[post >= 0].index
    recov_days = (recov[0] - end).days if len(recov) else None
    return {
        "max_drawdown": float(dd.min()),
        "start": str(start.date()),
        "end": str(end.date()),
        "recovery_days": int(recov_days) if recov_days is not None else None,
    }


def hit_rate(active: pd.Series) -> float:
    a = active.dropna()
    if len(a) == 0:
        return 0.0
    return float((a > 0).mean())


def tracking_error(active: pd.Series) -> float:
    return ann_vol(active)


def information_ratio(active: pd.Series) -> float:
    te = tracking_error(active)
    if te == 0:
        return 0.0
    if len(active) == 0:
        return 0.0
    mean_active = float(active.mean()) * TRADING_DAYS
    return mean_active / te


def calendar_year_returns(returns_df: pd.DataFrame) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for year, grp in returns_df.groupby(returns_df.index.year):
        row = {}
        for col in ["gross", "net", "benchmark", "active"]:
            if col not in grp.columns:
                continue
            if col == "active":
                row[col] = float(grp["net"].add(1).prod() - grp["benchmark"].add(1).prod())
            else:
                row[col] = float((1 + grp[col]).prod() - 1)
        out[int(year)] = row
    return out


def compute_summary(returns_df, weights_df, turnover_df, tcost_df, cfg) -> dict:
    gross = returns_df["gross"]
    net = returns_df["net"]
    bench = returns_df["benchmark"]
    active = returns_df["active"]

    years = len(returns_df) / TRADING_DAYS

    summary = {
        "cagr_gross": cagr(gross),
        "cagr_net": cagr(net),
        "cagr_benchmark": cagr(bench),
        "vol_gross": ann_vol(gross),
        "vol_net": ann_vol(net),
        "vol_benchmark": ann_vol(bench),
        "sharpe_gross": sharpe(gross),
        "sharpe_net": sharpe(net),
        "sharpe_benchmark": sharpe(bench),
        "sortino_net": sortino(net),
        "tracking_error": tracking_error(active),
        "information_ratio": information_ratio(active),
        "hit_rate_active": hit_rate(active),
        "avg_n_positions": float(
            (weights_df.drop(columns=["CASH"], errors="ignore") > 1e-9).sum(axis=1).mean()
        ),
        "turnover_per_rebal": float(turnover_df["one_way_turnover"].mean())
        if len(turnover_df) else 0.0,
        "turnover_annualized": float(turnover_df["one_way_turnover"].sum() / max(years, 1e-9))
        if len(turnover_df) else 0.0,
        "tcost_bps_per_rebal": float(tcost_df["drag_bps"].mean())
        if len(tcost_df) else 0.0,
        "tcost_bps_annualized": float(tcost_df["drag_bps"].sum() / max(years, 1e-9))
        if len(tcost_df) else 0.0,
        "calendar_year_returns": calendar_year_returns(returns_df),
    }

    for tag, series in [("gross", gross), ("net", net), ("benchmark", bench)]:
        dd = max_drawdown(series)
        summary[f"{tag}_max_drawdown"] = dd["max_drawdown"]
        summary[f"{tag}_max_drawdown_start"] = dd["start"]
        summary[f"{tag}_max_drawdown_end"] = dd["end"]
        summary[f"{tag}_max_drawdown_recovery_days"] = dd["recovery_days"]

    return summary
