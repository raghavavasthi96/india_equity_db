from __future__ import annotations

import argparse
import importlib.util
import json
import os
import webbrowser
from datetime import datetime

from .attribution import build_sector_frames
from .configs.config import BacktestConfig, PIPELINE_DATA_DIR, BACKTEST_DATA_DIR
from .engine import run_backtest


def _make_run_id(cfg: BacktestConfig) -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    parts = [stamp, cfg.weighting]
    if cfg.signal_name:
        # Make composite spec ("a:0.5,b:0.5") path-safe on Windows.
        safe = cfg.signal_name.replace(":", "=").replace(",", "+").replace(" ", "")
        parts.append(safe)
    parts.append(cfg.rebal_freq)
    return "_".join(parts)


def _input_fingerprints() -> dict:
    out = {}
    for f in ["prices_panel.csv", "universe_history.csv", "shares_outstanding.csv",
              "financials_panel.csv", "metadata.csv", "sector_classification.csv"]:
        p = os.path.join(PIPELINE_DATA_DIR, f)
        if os.path.exists(p):
            out[f] = {"mtime": datetime.utcfromtimestamp(os.path.getmtime(p)).isoformat(),
                      "size": os.path.getsize(p)}
    return out


def write_outputs(result, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "config.json"), "w") as f:
        payload = {
            "config": result.cfg.to_jsonable(),
            "inputs": _input_fingerprints(),
            "generated_at": datetime.utcnow().isoformat(),
        }
        json.dump(payload, f, indent=2, default=str)

    result.weights.to_csv(os.path.join(out_dir, "weights.csv"))
    if not result.diagnostics.empty:
        result.diagnostics.to_csv(os.path.join(out_dir, "rebalance_diagnostics.csv"), index=False)
    result.returns.to_csv(os.path.join(out_dir, "returns.csv"))
    if not result.turnover.empty:
        result.turnover.to_csv(os.path.join(out_dir, "turnover.csv"), index=False)
    if not result.tcost.empty:
        result.tcost.to_csv(os.path.join(out_dir, "tcost.csv"), index=False)

    # Must run before summary.json is written — it folds the sector stats in.
    attribution, active = build_sector_frames(result)
    if not attribution.empty:
        attribution.to_csv(os.path.join(out_dir, "sector_attribution.csv"), index=False)
    if not active.empty:
        active.to_csv(os.path.join(out_dir, "sector_active_weights.csv"), index=False)

    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(result.summary, f, indent=2, default=str)


def load_preset(path: str) -> tuple[BacktestConfig, bool]:
    """Load a preset Python file. Must define `cfg = BacktestConfig(...)` at
    module level. Optional `auto_open = True` opens the dashboard in the
    browser after rendering.
    """
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"Preset not found: {abs_path}")

    spec = importlib.util.spec_from_file_location("backtest_preset", abs_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load preset: {abs_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not hasattr(mod, "cfg"):
        raise AttributeError(
            f"{abs_path} must define `cfg = BacktestConfig(...)` at module level"
        )
    cfg = mod.cfg
    if not isinstance(cfg, BacktestConfig):
        raise TypeError(
            f"{abs_path}: `cfg` is {type(cfg).__name__}, expected BacktestConfig"
        )
    auto_open = bool(getattr(mod, "auto_open", False))
    return cfg, auto_open


def main():
    ap = argparse.ArgumentParser(
        description="Run a backtest from a preset Python config file. "
                    "Each preset must define `cfg = BacktestConfig(...)`. "
                    "See backtest/configs/ for examples.",
    )
    ap.add_argument("config_path",
                    help="Path to a preset Python file (e.g. backtest/configs/default.py)")
    args = ap.parse_args()

    cfg, auto_open = load_preset(args.config_path)
    if not cfg.run_id:
        cfg.run_id = _make_run_id(cfg)

    print(f"[run] preset={args.config_path}  run_id={cfg.run_id}  "
          f"weighting={cfg.weighting}  signal={cfg.signal_name}")

    result = run_backtest(cfg)

    out_dir = cfg.out_dir or os.path.join(
        BACKTEST_DATA_DIR, "backtests", cfg.run_id
    )
    write_outputs(result, out_dir)
    print(f"[done] outputs -> {out_dir}")

    try:
        from .dashboard import render_dashboard
        dash_path = os.path.join(out_dir, "dashboard.html")
        render_dashboard(result, dash_path)
        print(f"[dashboard] {dash_path}")
        if auto_open:
            webbrowser.open(f"file://{os.path.abspath(dash_path)}")
    except ImportError as e:
        print(f"[dashboard] skipped (plotly not installed): {e}")


if __name__ == "__main__":
    main()
