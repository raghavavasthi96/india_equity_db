from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional, Literal, Any
import json

from pipeline.configs.config import DATA_DIR as PIPELINE_DATA_DIR

_PKG_ROOT = os.path.dirname(os.path.dirname(__file__))  # backtest/
BACKTEST_DATA_DIR = os.path.join(_PKG_ROOT, "data")


@dataclass
class TcostConfig:
    """Per-name transaction cost in bps, computed from a composite cross-sectional
    illiquidity z-score (mcap + ADV + free-float). One-way cost applied on |Δw|
    at each rebalance.

        cost_bps(sym) = clip(base + k_illiq * max(0, illiq_z), floor_bps, ceiling_bps)
    """

    base: float = 15.0
    # Floor cost paid by even the most liquid name. Captures exchange fees,
    # brokerage, and minimum spread.

    k_illiq: float = 8.0
    # Slope on the positive illiquidity z-score. Higher = harsher penalty on
    # small/illiquid/tight-float names. ~8 means a +1σ illiquid name pays +8 bps.

    floor_bps: float = 10.0
    # Hard lower bound on per-name cost. Names cleaner than `base − floor` cannot
    # go below this.

    ceiling_bps: float = 150.0
    # Hard upper bound. Caps tail micro-caps from dominating realised costs.

    def to_dict(self) -> dict:
        return asdict(self)


WeightingMode = Literal["equal", "ffmcap", "score_weighted", "ffmcap_tilt"]


@dataclass
class BacktestConfig:
    """All tunable knobs for one backtest run. Defaults yield a benchmark-like
    portfolio: quarterly ff-mcap of the top-500 PIT universe, 20% stock cap, no
    signal, no cash buffer.

    Field groupings: window, rebalance, weighting, signal, caps, universe filters,
    cost/cash, reporting.
    """

    # ----- Window ------------------------------------------------------------
    start: Optional[str] = "2018-06-30"
    # ISO date string. Backtest start. None = use first trading date in panel.

    end: Optional[str] = "2026-04-30"
    # ISO date string. Backtest end. None = use last trading date.

    # ----- Rebalance schedule -----------------------------------------------
    rebal_freq: str = "Q"
    # "M" / "Q" / "A" → calendar month-/quarter-/year-end snapped to the prior
    # trading day. Use "custom" with `custom_rebal_dates` for explicit dates.

    custom_rebal_dates: Optional[list] = None
    # List of date strings/Timestamps. Only used when rebal_freq == "custom".

    rebal_offset: int = 1
    # Trading-day gap between snapshot date and execution date. Default T+1
    # prevents look-ahead: snapshot at close of `t`, trade at close of `t+1`.

    # ----- Weighting --------------------------------------------------------
    weighting: WeightingMode = "ffmcap"
    # "equal"          → 1/N across eligible pool.
    # "ffmcap"         → proportional to free-float market cap (default).
    # "score_weighted" → proportional to max(0, z − score_threshold). Needs signal_fn.
    # "ffmcap_tilt"    → ff_mcap × exp(tilt_gamma × z). Needs signal_fn.

    tilt_gamma: float = 0.5
    # Tilt strength for "ffmcap_tilt". 0 = pure ff-mcap, 1 = ~e× weight on +1σ
    # names. Negative tilts the opposite direction. Only used if
    # weighting=="ffmcap_tilt".

    score_threshold: float = 0.0
    # Z-score floor for "score_weighted": names with z ≤ threshold get 0 weight.
    # Raise above 0 to concentrate in the top tail.

    # ----- Signal -----------------------------------------------------------
    signal_fn: Optional[Callable] = None
    # Callable `(pool_symbols, date, ctx) → pd.Series`. Output standardised
    # (winsorise → z-score) before use. None = no signal (only valid with
    # "equal" or "ffmcap" weighting).

    signal_name: Optional[str] = None
    # Human-readable name used in run_id, dashboard header, and outputs.
    # Auto-set when invoked via CLI from SIGNAL_REGISTRY.

    signal_top_n: Optional[int] = None
    # Post-signal selection: keep only top N names by z-score before weighting.
    # Mutually exclusive with signal_top_quantile. None = keep all.

    signal_top_quantile: Optional[float] = None
    # Post-signal selection: keep top quantile (e.g. 0.3 = top 30%). None = keep all.

    winsor_sigma: float = 3.0
    # Clip raw signal at ±winsor_sigma standard deviations before z-scoring.
    # Stops single-name outliers from dominating the cross-section.

    # ----- Caps -------------------------------------------------------------
    max_stock_wt: float = 0.20
    # Per-name weight cap. None disables. Engine raises if max_stock_wt × N < 1.

    max_sector_wt: Optional[float] = None
    # Per-sector weight cap. Stub: requires sector_map_csv; logs warning if absent.

    sector_map_csv: Optional[str] = None
    # Path to CSV with columns `symbol,sector`. Loaded once at context build.

    max_size_bucket_wt: Optional[dict] = None
    # Dict `{bucket_name: cap}`, e.g. {"small": 0.20}. Buckets per `size_buckets`.

    size_buckets: dict = field(
        default_factory=lambda: {
            "large": (1, 100),
            "mid": (101, 250),
            "small": (251, None),
        }
    )
    # PIT mcap-rank ranges (inclusive lower, inclusive upper; None = unbounded).
    # Only used when max_size_bucket_wt is set.

    # ----- Universe filters (pre-signal) ------------------------------------
    min_price: float = 1.0
    # Drop names with PIT close below this INR floor. 0 disables.

    min_adv_inr: Optional[float] = None
    # Drop names with 30-day median INR-volume below this threshold. None disables.

    min_history_days: int = 0
    # Drop names with fewer than N non-NaN closes prior to rebal date. Useful for
    # excluding recent IPOs from signal-driven strategies.

    max_universe_rank: Optional[int] = None
    # Restrict to top-N of the PIT universe by mcap rank. None = full universe.

    min_free_float_pct: Optional[float] = None
    # Drop names where free_float_shares / shares_outstanding < threshold (0-1).

    exclude_symbols: list = field(default_factory=list)
    # Hard blacklist; symbols dropped pre-signal.

    extra_filters: list = field(default_factory=list)
    # User callables `(pool, date, ctx) → pool`. Run after the built-in pre-signal
    # stack. Use for ad-hoc constraints (e.g. sector exclusions).

    # ----- Cost / cash ------------------------------------------------------
    tcost: TcostConfig = field(default_factory=TcostConfig)
    # See TcostConfig. Applied as gross-NAV haircut on each rebalance via |Δw|.

    cash_buffer: float = 0.0
    # Fixed cash sleeve (0-1). Applied last: investable weights scaled to
    # (1 − cash_buffer), remainder held in synthetic `CASH` ticker (r=0).

    # ----- Benchmark / reporting --------------------------------------------
    benchmark: str = "nifty500_tri"
    # Identifier shown in dashboard/outputs. Currently only "nifty500_tri" is
    # supported — externally-sourced Nifty 500 Total Returns Index loaded from
    # `benchmark_csv`. Used for IR/TE/active calculations.

    benchmark_csv: Optional[str] = None
    # Path to CSV with columns `date,close` (TRI level). None = use the project
    # default `backtest/data/nifty500_tri.csv`. Seed/refresh with:
    #     python -m backtest.utils.benchmark --start 2018-01-01

    rolling_window_days: int = 252
    # Window length for rolling-stats panels in the dashboard (return, vol,
    # Sharpe, IR). Default = ~1 trading year.

    # ----- Identification ---------------------------------------------------
    run_id: Optional[str] = None
    # Subdirectory name under data/backtests/. Auto-generated from timestamp +
    # weighting + signal + freq if not provided.

    out_dir: Optional[str] = None
    # Override the output directory. None = use `data/backtests/<run_id>/`.
    # Useful for grouping related runs under a shared parent folder.

    # ----- Serialisation ----------------------------------------------------
    def to_jsonable(self) -> dict:
        d: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if callable(v):
                d[k] = getattr(v, "__name__", repr(v))
            else:
                d[k] = v
        d["signal_fn"] = self.signal_name or (
            self.signal_fn.__name__ if self.signal_fn else None
        )
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_jsonable(), indent=2, default=str)
