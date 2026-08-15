"""
run_pipeline.py — Run the india_equity_db pipeline. Stops on first failure.

Modes (mirrors docs/README.md):
    smoke      Smoke test on 3 symbols (RELIANCE,TCS,ZYDUSWELL). All 7 steps.
    full       Full 500-symbol run. All 7 steps.
    daily      Daily incremental: fetch_shp_xbrl + fetch_prices + validate.
    quarterly  Quarterly refresh. Alias for `full` (same script ordering).
    force      Full run with --force on fetch_financials, fetch_shp_xbrl and
               fetch_sectors (re-download all screener HTML, XBRL files and the
               BSE scrip master; rebuild universe from scratch).

Sector classification (step 5) is excluded from `daily` — industry
classifications change on the order of once a year, not once a day.

Usage:
    python -m pipeline.run_pipeline --mode smoke
    python -m pipeline.run_pipeline --mode full
    python -m pipeline.run_pipeline --mode daily
    python -m pipeline.run_pipeline --mode quarterly
    python -m pipeline.run_pipeline --mode force
    python -m pipeline.run_pipeline --mode full --symbols RELIANCE,TCS    # override subset

Parallel workers for fetch_shp_xbrl.py and fetch_financials.py are set in
config.py via XBRL_WORKERS and FINANCIALS_WORKERS. To override ad-hoc, run
the individual script with --workers N.

Bootstrap (survivorship-bias-free builds):
    Step 1 (universe.py --bootstrap --refresh-universe) scans bhavcopy_cache/ to
    build data/symbol_history.csv before constructing raw_universe.csv. On a cold
    cache it auto-warms by fetching quarter-end bhavcopies across
    PROJECT_START_DATE..today (~5–10 min extra on the first run). No manual
    pre-warming step required.

Price fallback (delisted/merged symbols):
    Step 5 (fetch_prices.py) downloads via yfinance first, then for any symbol
    yfinance returns empty (typically merged/delisted, e.g. HDFC post-merger,
    JETAIRWAYS, DHFL), falls back to daily NSE bhavcopies back-adjusted with
    splits/bonuses/dividends from NSE corporate-actions API. Cold fallback adds
    ~15–30 min per affected symbol; cached after first run. Use --no-fallback
    on fetch_prices.py to restore yfinance-only behavior.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

# sys is kept solely for sys.executable; exit propagation uses SystemExit.


SMOKE_SYMBOLS = "RELIANCE,TCS,ZYDUSWELL"


PROJECT_ROOT = Path(__file__).resolve().parent.parent  # india_equity_db/


def run(label: str, cmd: list) -> None:
    print(f"\n{'=' * 70}\n[{label}] {' '.join(cmd)}\n{'=' * 70}", flush=True)
    t0 = time.time()
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    elapsed = time.time() - t0
    if result.returncode != 0:
        print(f"\n[{label}] FAILED after {elapsed:.1f}s (exit {result.returncode})", flush=True)
        raise SystemExit(result.returncode)
    print(f"\n[{label}] OK ({elapsed:.1f}s)", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Run the india_equity_db pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mode", required=True,
                        choices=["smoke", "full", "daily", "quarterly", "force"],
                        help="Which pipeline mode to run.")
    parser.add_argument("--symbols", type=str, default=None,
                        help="Override symbol subset (comma-separated). "
                             "Defaults: smoke=RELIANCE,TCS,ZYDUSWELL; others=all.")
    args = parser.parse_args()

    py = sys.executable

    if args.symbols is not None:
        symbols = args.symbols
    elif args.mode == "smoke":
        symbols = SMOKE_SYMBOLS
    else:
        symbols = None

    sym_args = ["--symbols", symbols] if symbols else []

    t_start = time.time()

    if args.mode == "daily":
        run("1/3 fetch_shp_xbrl", [py, "-m", "pipeline.fetch_shp_xbrl"] + sym_args)
        run("2/3 fetch_prices",   [py, "-m", "pipeline.fetch_prices"] + sym_args)
        run("3/3 validate",       [py, "-m", "pipeline.validate"])
    else:
        # smoke | full | quarterly | force — same 7-step ordering
        run("1/7 universe bootstrap",
            [py, "-m", "pipeline.universe", "--bootstrap", "--refresh-universe"] + sym_args)

        xbrl_args = [py, "-m", "pipeline.fetch_shp_xbrl"] + sym_args
        if args.mode == "force":
            xbrl_args.append("--force")
        run("2/7 fetch_shp_xbrl", xbrl_args)

        run("3/7 universe ranking", [py, "-m", "pipeline.universe"] + sym_args)

        fin_args = [py, "-m", "pipeline.fetch_financials"] + sym_args
        if args.mode == "force":
            fin_args.append("--force")
        run("4/7 fetch_financials", fin_args)

        # Reads the screener cache fetch_financials just refreshed, so it must
        # follow step 4.
        sec_args = [py, "-m", "pipeline.fetch_sectors"] + sym_args
        if args.mode == "force":
            sec_args.append("--force")
        run("5/7 fetch_sectors", sec_args)

        run("6/7 fetch_prices",     [py, "-m", "pipeline.fetch_prices"] + sym_args)
        run("7/7 validate",         [py, "-m", "pipeline.validate"])

    total = time.time() - t_start
    print(f"\nPipeline complete in {total:.1f}s (mode={args.mode}).", flush=True)


if __name__ == "__main__":
    main()
