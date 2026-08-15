from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from backtest.configs.config import BacktestConfig, PIPELINE_DATA_DIR, BACKTEST_DATA_DIR

CACHE_DIR = os.path.join(BACKTEST_DATA_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

CASH = "CASH"


def _cache_path(name: str) -> str:
    return os.path.join(CACHE_DIR, f"{name}.parquet")


def _is_stale(cache_file: str, source_file: str) -> bool:
    if not os.path.exists(cache_file):
        return True
    return os.path.getmtime(cache_file) < os.path.getmtime(source_file)


def _pivot_param(panel: pd.DataFrame, param: str) -> pd.DataFrame:
    sub = panel[panel["parameter"] == param]
    w = sub.pivot(index="date", columns="symbol", values="value")
    w.index = pd.to_datetime(w.index)
    return w.sort_index()


def load_close_wide() -> pd.DataFrame:
    src = os.path.join(PIPELINE_DATA_DIR, "prices_panel.csv")
    cache = _cache_path("close_wide")
    if not _is_stale(cache, src):
        return pd.read_parquet(cache)
    panel = pd.read_csv(src)
    w = _pivot_param(panel, "close")
    w.to_parquet(cache)
    return w


def load_volume_wide() -> pd.DataFrame:
    src = os.path.join(PIPELINE_DATA_DIR, "prices_panel.csv")
    cache = _cache_path("volume_wide")
    if not _is_stale(cache, src):
        return pd.read_parquet(cache)
    panel = pd.read_csv(src)
    w = _pivot_param(panel, "volume")
    w.to_parquet(cache)
    return w


def compute_adv_30d_inr(close: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    src = os.path.join(PIPELINE_DATA_DIR, "prices_panel.csv")
    cache = _cache_path("adv_30d_inr")
    if not _is_stale(cache, src):
        return pd.read_parquet(cache)
    common_cols = close.columns.intersection(volume.columns)
    common_idx = close.index.intersection(volume.index)
    notional = close.loc[common_idx, common_cols] * volume.loc[common_idx, common_cols]
    adv = notional.rolling(window=30, min_periods=10).median()
    adv.to_parquet(cache)
    return adv


def load_universe_history() -> pd.DataFrame:
    src = os.path.join(PIPELINE_DATA_DIR, "universe_history.csv")
    df = pd.read_csv(src, parse_dates=["quarter_end_date"])
    return df.sort_values(["quarter_end_date", "rank"]).reset_index(drop=True)


def load_shares() -> pd.DataFrame:
    src = os.path.join(PIPELINE_DATA_DIR, "shares_outstanding.csv")
    return pd.read_csv(src, parse_dates=["date"])


def load_metadata() -> pd.DataFrame:
    src = os.path.join(PIPELINE_DATA_DIR, "metadata.csv")
    return pd.read_csv(src)


def load_financials() -> pd.DataFrame:
    src = os.path.join(PIPELINE_DATA_DIR, "financials_panel.csv")
    return pd.read_csv(src, parse_dates=["date"])


SECTOR_CLASSIFICATION_PATH = os.path.join(PIPELINE_DATA_DIR, "sector_classification.csv")

SECTOR_LEVELS = ["macro_sector", "sector", "industry", "basic_industry"]


def load_sector_map(path: Optional[str], level: str = "sector") -> Optional[dict]:
    """
    symbol -> classification at `level`, from the pipeline's
    `sector_classification.csv` (or any CSV with a `symbol` column and that level).

    Returns None only when the file genuinely does not exist, so the sector cap
    and dashboard breakdown keep their existing "not configured" degradation.
    """
    if level not in SECTOR_LEVELS:
        raise ValueError(f"sector_level must be one of {SECTOR_LEVELS}, got {level!r}")

    src = path or SECTOR_CLASSIFICATION_PATH
    if not os.path.exists(src):
        if path:
            raise FileNotFoundError(f"sector_map_csv not found: {src}")
        return None

    df = pd.read_csv(src)
    if "symbol" not in df.columns:
        raise ValueError(f"{src}: missing required `symbol` column")
    if level in df.columns:
        col = level
    elif "sector" in df.columns:
        # Legacy two-column `symbol,sector` map — only one level available.
        col = "sector"
    else:
        raise ValueError(
            f"{src}: has neither a {level!r} nor a 'sector' column. "
            f"Regenerate it with `python -m pipeline.fetch_sectors`."
        )
    return dict(zip(df["symbol"], df[col]))


def benchmark_weights_asof(ctx: "BacktestContext", date: pd.Timestamp) -> pd.Series:
    """
    Free-float-mcap weights of the PIT universe snapshot at `date` — the
    `ffmcap_top500` weight benchmark used for active-weight reporting.

    Note this is NOT the return benchmark (`nifty500_tri`): the TRI file carries
    index levels only, never constituents, so true index weights are unavailable.
    Anything derived from this must be labelled as vs ff-mcap top-500.
    """
    snap = ctx.snap_asof(date)
    if snap.empty:
        return pd.Series(dtype=float)
    w = snap["free_float_market_cap_inr"].astype(float)
    w = w[w > 0]
    if w.empty:
        return pd.Series(dtype=float)
    return w / w.sum()


@dataclass
class BacktestContext:
    cfg: BacktestConfig
    close: pd.DataFrame                 # date x symbol (ffilled)
    returns: pd.DataFrame               # date x symbol (fillna 0)
    volume: pd.DataFrame                # raw
    adv_30d_inr: pd.DataFrame           # date x symbol
    universe_history: pd.DataFrame      # long
    universe_by_date: dict              # quarter_end_date -> DataFrame indexed by symbol
    shares: pd.DataFrame
    metadata: pd.DataFrame
    sector_map: Optional[dict]
    last_valid_date: pd.Series          # symbol -> last date with a non-NaN close
    trading_dates: pd.DatetimeIndex
    rebal_quarter_ends: pd.DatetimeIndex
    _financials: Optional[pd.DataFrame] = None

    @property
    def financials(self) -> pd.DataFrame:
        if self._financials is None:
            self._financials = load_financials()
        return self._financials

    def snap_asof(self, date: pd.Timestamp) -> pd.DataFrame:
        # Latest quarterly snapshot strictly <= date
        valid = self.rebal_quarter_ends[self.rebal_quarter_ends <= date]
        if len(valid) == 0:
            return pd.DataFrame()
        return self.universe_by_date[valid[-1]]


def build_context(cfg: BacktestConfig) -> BacktestContext:
    close_raw = load_close_wide()
    volume = load_volume_wide()
    adv = compute_adv_30d_inr(close_raw, volume)
    uh = load_universe_history()
    shares = load_shares()
    metadata = load_metadata()
    sector_map = load_sector_map(cfg.sector_map_csv, cfg.sector_level)

    # Last valid close date per symbol BEFORE ffill (delisting marker)
    last_valid = close_raw.apply(lambda s: s.last_valid_index())
    last_valid = last_valid.dropna()

    # ffill close to handle holidays/intermittent gaps. Delistings handled in engine.
    close = close_raw.ffill()
    returns = close.pct_change().fillna(0.0)

    universe_by_date = {
        qed: g.set_index("symbol")
        for qed, g in uh.groupby("quarter_end_date")
    }
    quarter_ends = pd.DatetimeIndex(sorted(universe_by_date.keys()))

    return BacktestContext(
        cfg=cfg,
        close=close,
        returns=returns,
        volume=volume,
        adv_30d_inr=adv,
        universe_history=uh,
        universe_by_date=universe_by_date,
        shares=shares,
        metadata=metadata,
        sector_map=sector_map,
        last_valid_date=last_valid,
        trading_dates=close.index,
        rebal_quarter_ends=quarter_ends,
    )


def build_rebal_schedule(cfg: BacktestConfig, ctx: BacktestContext) -> pd.DatetimeIndex:
    start = pd.Timestamp(cfg.start) if cfg.start else ctx.trading_dates[0]
    end = pd.Timestamp(cfg.end) if cfg.end else ctx.trading_dates[-1]

    if cfg.rebal_freq == "custom":
        dates = pd.DatetimeIndex(sorted(pd.to_datetime(cfg.custom_rebal_dates)))
        return dates[(dates >= start) & (dates <= end)]

    freq_map = {"M": "ME", "Q": "QE", "A": "YE"}
    pandas_freq = freq_map.get(cfg.rebal_freq, cfg.rebal_freq)
    candidates = pd.date_range(start=start, end=end, freq=pandas_freq)
    # Snap each candidate to nearest trading date <= candidate. Two fall-forward
    # cases override the default snap-backward:
    #   (a) no trading date <= candidate (candidate predates price panel);
    #   (b) snap-backward lands strictly before the first universe snapshot
    #       (e.g. candidate = 2018-06-30 Sat snaps back to 2018-06-29 Fri, but
    #       the first universe snapshot is 2018-06-30 — snap_asof would return
    #       empty and the rebal would produce no target).
    # Both cases land on the first trading date >= candidate, keeping all rebal
    # frequencies that share a `cfg.start` seeded on the same day.
    first_snap = (
        ctx.rebal_quarter_ends[0] if len(ctx.rebal_quarter_ends) else None
    )
    snapped = []
    for d in candidates:
        eligible = ctx.trading_dates[ctx.trading_dates <= d]
        if len(eligible) > 0:
            cand = eligible[-1]
            if first_snap is not None and cand < first_snap:
                forward = ctx.trading_dates[ctx.trading_dates >= d]
                if len(forward) > 0:
                    cand = forward[0]
            snapped.append(cand)
        else:
            forward = ctx.trading_dates[ctx.trading_dates >= d]
            if len(forward) > 0:
                snapped.append(forward[0])
    return pd.DatetimeIndex(snapped).unique()


def trade_date_for(rebal_date: pd.Timestamp, offset: int, ctx: BacktestContext) -> pd.Timestamp:
    idx = ctx.trading_dates.get_indexer([rebal_date])[0]
    target = idx + offset
    if target >= len(ctx.trading_dates):
        return ctx.trading_dates[-1]
    return ctx.trading_dates[target]
