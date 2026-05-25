"""
utils/gaps.py — Unified failure-log writer.

Every script that detects an unrecoverable problem for a symbol appends one row to
`data/data_gaps.csv` via `log_gap(...)`. Columns:

    script         — fetch_financials | fetch_shp_xbrl | fetch_prices | universe
    symbol         — NSE symbol
    reason         — short categorical code (e.g. PageFetchFailed, xbrl_unavailable_for_delisted)
    error_msg      — free-text detail (optional)
    recorded_date  — ISO date the row was written

Single source of truth for downstream review and validation.
"""

from datetime import date

from .io import append_to_csv, path


GAPS_FILE = "data_gaps.csv"
GAPS_COLS = ["script", "symbol", "reason", "error_msg", "recorded_date"]


def log_gap(script: str, symbol: str, reason: str, error_msg: str = "") -> None:
    append_to_csv({
        "script": script,
        "symbol": symbol,
        "reason": reason,
        "error_msg": error_msg,
        "recorded_date": date.today().isoformat(),
    }, path(GAPS_FILE))
