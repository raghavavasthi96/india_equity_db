"""
validate.py — Post-run sanity checks for the India Equity Database.

Run:
    python -m pipeline.validate

Writes data/validation_report.txt. Exits non-zero if any CRITICAL check fails.

Panel format expected: long CSV with columns (date, symbol, parameter, value).
"""

import os
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from pipeline.configs import config
from .utils.logger import get_logger
from .utils.io import read_long_panel, path

logger = get_logger("validate")

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


class CheckResult:
    def __init__(self, name: str, status: str, detail: str):
        self.name = name
        self.status = status
        self.detail = detail

    def __repr__(self):
        return f"{self.name:<48} {self.status:<6} {self.detail}"


class Validator:
    def __init__(self):
        self.results: list = []

    def check(self, name: str, cond: bool, warn_cond: bool = False, detail: str = "") -> str:
        status = PASS if cond else (WARN if warn_cond else FAIL)
        r = CheckResult(name, status, detail)
        self.results.append(r)
        logger.info(str(r))
        return status

    def record(self, name: str, status: str, detail: str = "") -> None:
        r = CheckResult(name, status, detail)
        self.results.append(r)
        logger.info(str(r))


def load_csv_optional(filepath: str, **kwargs):
    if not os.path.exists(filepath):
        return None
    return pd.read_csv(filepath, **kwargs)


# ---------------------------------------------------------------------------
# Shares outstanding checks
# ---------------------------------------------------------------------------

def check_shares_outstanding(v: Validator):
    shares_path = path("shares_outstanding.csv")
    meta_path = path("metadata.csv")

    shares = load_csv_optional(shares_path, parse_dates=["date"])
    meta = load_csv_optional(meta_path)

    if shares is None:
        v.record("shares_outstanding_exists", FAIL, "File missing")
        return

    required_cols = {"symbol", "date", "total_shares", "promoter_shares", "free_float_shares"}
    missing_cols = required_cols - set(shares.columns)
    if missing_cols:
        v.record("shares_outstanding_schema", FAIL, f"Missing columns: {missing_cols}")
        return

    # Real integrity check: total ≈ promoter + public + non_pub_nonp (additive, sourced from XBRL)
    # Skip rows missing the additive columns (legacy rows pre-Phase-3 schema).
    if "public_shares" in shares.columns and "non_pub_nonp_shares" in shares.columns:
        complete = shares.dropna(subset=["public_shares", "non_pub_nonp_shares"])
        if not complete.empty:
            additive = complete["promoter_shares"] + complete["public_shares"] + complete["non_pub_nonp_shares"]
            bad_rows = complete[(additive - complete["total_shares"]).abs() > 1]
            v.check("shares_integrity_additive", len(bad_rows) == 0,
                    warn_cond=len(bad_rows) <= 5,
                    detail=f"{len(bad_rows)}/{len(complete)} rows where promoter+public+non_pub_nonp != total")

    bad_neg = shares[
        (shares["free_float_shares"] <= 0) |
        (shares["free_float_shares"] > shares["total_shares"]) |
        (shares["promoter_shares"] < 0)
    ]
    v.check("shares_integrity_bounds", len(bad_neg) == 0,
            detail=f"{len(bad_neg)} rows with out-of-bounds free_float or negative promoter")

    if meta is not None:
        project_start = pd.Timestamp(config.PROJECT_START_DATE)
        shares_in_window = shares[pd.to_datetime(shares["date"]) >= project_start]
        filings_per_sym = shares_in_window.groupby("symbol")["date"].count()
        meta_syms = set(meta["symbol"].dropna().unique())

        zero_xbrl = [s for s in meta_syms if s not in filings_per_sym.index]
        if zero_xbrl:
            v.record("shares_zero_xbrl_symbols", WARN,
                     f"{len(zero_xbrl)} metadata symbols with 0 XBRL filings: {zero_xbrl[:5]}")


# ---------------------------------------------------------------------------
# Universe checks
# ---------------------------------------------------------------------------

def check_universe(v: Validator):
    uh_path = path("universe_history.csv")
    meta_path = path("metadata.csv")

    uh = load_csv_optional(uh_path, parse_dates=["quarter_end_date"])
    meta = load_csv_optional(meta_path)

    if uh is None:
        v.record("universe_history_exists", FAIL, "File missing")
        return
    if meta is None:
        v.record("metadata_exists", FAIL, "File missing")
        return

    n_quarters = uh["quarter_end_date"].nunique()
    v.check("universe_quarter_count", n_quarters >= 28, warn_cond=n_quarters >= 20, detail=f"{n_quarters} quarters")

    sizes = uh.groupby("quarter_end_date")["symbol"].count()
    min_sz, max_sz = int(sizes.min()), int(sizes.max())
    v.check("universe_size_per_quarter", min_sz >= 490, warn_cond=min_sz >= 480, detail=f"min={min_sz} max={max_sz}")

    dup = uh.duplicated(subset=["quarter_end_date", "symbol"]).sum()
    v.check("universe_no_duplicates", dup == 0, detail=f"{dup} duplicates")

    n_meta = len(meta)
    v.check("metadata_symbol_count", 500 <= n_meta <= 1500, warn_cond=n_meta > 0, detail=f"{n_meta} symbols")

    uh_syms = set(uh["symbol"].unique())
    meta_syms = set(meta["symbol"].unique())
    missing = uh_syms - meta_syms
    v.check("universe_symbols_in_metadata", len(missing) == 0, warn_cond=len(missing) <= 10,
            detail=f"{len(missing)} missing" + (f": {list(missing)[:5]}" if missing else ""))


# ---------------------------------------------------------------------------
# Price panel checks
# ---------------------------------------------------------------------------

def check_prices(v: Validator):
    meta_path = path("metadata.csv")
    prices_path = path("prices_panel.csv")

    meta = load_csv_optional(meta_path)
    panel = read_long_panel(prices_path)

    if panel is None or panel.empty:
        v.record("prices_panel_exists", FAIL, "File missing or empty")
        return
    if meta is None:
        v.record("metadata_exists_for_prices", FAIL, "metadata.csv missing")
        return

    meta_syms = set(meta["symbol"].dropna().unique())
    panel_syms = set(panel["symbol"].unique())

    # Symbol coverage
    missing = meta_syms - panel_syms
    pct_present = 100 * len(panel_syms & meta_syms) / max(len(meta_syms), 1)
    v.check("price_symbols_coverage", pct_present >= 95, warn_cond=pct_present >= 80,
            detail=f"{pct_present:.1f}% present; {len(missing)} missing")

    # 5 fields per symbol
    fields_per_sym = panel.groupby("symbol")["parameter"].nunique()
    bad_fields = (fields_per_sym != 5).sum()
    v.check("price_fields_per_symbol", bad_fields == 0, warn_cond=bad_fields <= 5,
            detail=f"{bad_fields} symbols with != 5 fields")

    # Date index monotonic & no duplicates (per symbol)
    dup_rows = panel.duplicated(subset=["date", "symbol", "parameter"]).sum()
    v.check("price_no_duplicate_rows", dup_rows == 0, warn_cond=dup_rows <= 10,
            detail=f"{dup_rows} duplicate (date, symbol, parameter) rows")

    # Close coverage >= 80% in active window — full panel, vectorized.
    close_panel = panel[panel["parameter"] == "close"].copy()
    close_panel["date"] = pd.to_datetime(close_panel["date"])
    meta_windows = meta.set_index("symbol")[["first_in_universe_date", "last_in_universe_date"]].copy()
    meta_windows["first_in_universe_date"] = pd.to_datetime(meta_windows["first_in_universe_date"], errors="coerce")
    meta_windows["last_in_universe_date"] = pd.to_datetime(meta_windows["last_in_universe_date"], errors="coerce")
    valid_windows = meta_windows.dropna()
    if not valid_windows.empty:
        merged = close_panel.merge(
            valid_windows, left_on="symbol", right_index=True, how="inner"
        )
        in_window = merged[
            (merged["date"] >= merged["first_in_universe_date"]) &
            (merged["date"] <= merged["last_in_universe_date"])
        ]
        rows_per_sym = in_window.groupby("symbol")["date"].count()
        window_days = (valid_windows["last_in_universe_date"] - valid_windows["first_in_universe_date"]).dt.days + 1
        trading_est = (window_days * 5 / 7).clip(lower=1)
        coverage = (rows_per_sym / trading_est).dropna()
        low_cov = coverage[coverage < 0.80].index.tolist()
    else:
        low_cov = []
    v.check("price_close_coverage_80pct", len(low_cov) == 0, warn_cond=len(low_cov) <= 5,
            detail=f"{len(low_cov)} symbols below 80% close coverage (full panel)")

    # No negative prices
    price_params = ["open", "high", "low", "close"]
    neg = panel[panel["parameter"].isin(price_params) & (panel["value"] < 0)]
    v.check("price_no_negatives", len(neg) == 0, warn_cond=len(neg) <= 5,
            detail=f"{len(neg)} negative price values")

    # OHLC sanity — full panel pivot.
    ohlc_long = panel[panel["parameter"].isin(price_params)]
    violations = 0
    if not ohlc_long.empty:
        wide = ohlc_long.pivot_table(
            index=["date", "symbol"], columns="parameter", values="value", aggfunc="last"
        )
        if all(c in wide.columns for c in ("open", "high", "low", "close")):
            wide = wide.dropna(subset=["open", "high", "low", "close"])
            o, h, l, c_ = wide["open"], wide["high"], wide["low"], wide["close"]
            violations = int(((h < l) | (h < o) | (h < c_) | (l > o) | (l > c_)).sum())
    v.check("price_ohlc_sanity", violations == 0, warn_cond=violations <= 10,
            detail=f"{violations} OHLC violations (full panel)")

    # Outlier returns >50% — full panel.
    outlier_path = path("validation_outliers.csv")
    sorted_close = close_panel.sort_values(["symbol", "date"])
    sorted_close["abs_return"] = sorted_close.groupby("symbol")["value"].pct_change().abs()
    big_moves = sorted_close[sorted_close["abs_return"] > 0.50][["symbol", "date", "abs_return"]].copy()
    if not big_moves.empty:
        big_moves["abs_return"] = big_moves["abs_return"].round(4)
        big_moves.to_csv(outlier_path, index=False)
    v.record("price_return_outliers",
             WARN if not big_moves.empty else PASS,
             f"{len(big_moves)} moves >50% (full panel); logged to validation_outliers.csv")

    # Staleness
    last_date = pd.to_datetime(panel["date"]).max()
    days_stale = (pd.Timestamp(date.today()) - last_date).days
    v.check("price_data_staleness", days_stale <= 5, warn_cond=days_stale <= 14,
            detail=f"Last date {last_date.date()} ({days_stale}d ago)")

    # Bhavcopy fallback: among UNIVERSE symbols (in metadata) flagged as delisted-no-xbrl,
    # confirm they have price rows. Exclude symbols also flagged with a fallback failure reason.
    # Non-universe symbols in data_gaps are irrelevant — fetch_prices never tried them.
    gaps_path = path("data_gaps.csv")
    if os.path.exists(gaps_path):
        gaps = pd.read_csv(gaps_path)
        delisted = set(gaps[gaps["reason"] == "xbrl_unavailable_for_delisted"]["symbol"])
        unrecoverable = set(gaps[gaps["reason"].isin(["bhavcopy_no_rows_in_range", "no_corp_actions_for_fallback"])]["symbol"])
        candidates = (delisted & meta_syms) - unrecoverable
        recovered = candidates & panel_syms
        pct = 100 * len(recovered) / max(len(candidates), 1) if candidates else 100.0
        v.check("prices_fallback_coverage", pct >= 80, warn_cond=pct >= 50,
                detail=f"{len(recovered)}/{len(candidates)} universe-delisted symbols recovered via bhavcopy fallback ({pct:.0f}%)")

    # Corp action adjustments file integrity
    adj_path = path("corporate_action_adjustments.csv")
    if os.path.exists(adj_path):
        adj = pd.read_csv(adj_path)
        bad_types = adj[~adj["action_type"].isin(["split", "bonus", "dividend"])]
        v.check("corp_action_adjustment_types", len(bad_types) == 0,
                detail=f"{len(bad_types)} rows with unknown action_type")
        # Sample: for each fallback symbol with adjustments, prices should exist in panel
        adj_syms = set(adj["symbol"].unique())
        missing_in_panel = adj_syms - panel_syms
        v.check("corp_action_adjustment_symbol_coverage", len(missing_in_panel) == 0,
                warn_cond=len(missing_in_panel) <= 2,
                detail=f"{len(missing_in_panel)} symbols in adjustments file absent from prices_panel")


# ---------------------------------------------------------------------------
# Financials panel checks
# ---------------------------------------------------------------------------

def _load_script_gaps(script: str) -> Optional[pd.DataFrame]:
    """Read rows in data_gaps.csv tagged with the given script. None if file missing."""
    gaps_path = path("data_gaps.csv")
    if not os.path.exists(gaps_path):
        return None
    gaps = pd.read_csv(gaps_path)
    return gaps[gaps["script"] == script]


def check_financials(v: Validator):
    meta_path = path("metadata.csv")
    q_panel_path = path("financials_panel.csv")

    meta = load_csv_optional(meta_path)
    panel = read_long_panel(q_panel_path)
    failed = _load_script_gaps("fetch_financials")

    if meta is None:
        return

    if panel is None or panel.empty:
        v.record("financials_panel_exists", FAIL, "File missing or empty")
        return

    panel_syms = set(panel["symbol"].unique())
    meta_syms = set(meta["symbol"].dropna().unique())
    failed_syms = set(failed["symbol"].unique()) if failed is not None else set()

    no_data = meta_syms - panel_syms - failed_syms
    v.check("financials_symbol_coverage", len(no_data) == 0, warn_cond=len(no_data) <= 10,
            detail=f"{len(no_data)} symbols with no data and not in failed log")

    # Quarter dates align to Mar/Jun/Sep/Dec
    panel["date"] = pd.to_datetime(panel["date"])
    months_ok = panel["date"].dt.month.isin([3, 6, 9, 12])
    pct_aligned = 100 * months_ok.mean()
    v.check("financials_quarter_alignment", pct_aligned >= 90, warn_cond=pct_aligned >= 75,
            detail=f"{pct_aligned:.1f}% of dates in Mar/Jun/Sep/Dec")

    # Core line items present — full panel, set-based.
    # Universal across all sectors: total_revenue, net_profit, eps.
    # Profitability metric: ebitda (non-financials) OR financing_profit (banks/NBFCs).
    core_universal = ["total_revenue", "net_profit", "eps"]
    profitability_metrics = ["ebitda", "financing_profit"]

    params_by_sym = panel.groupby("symbol")["parameter"].apply(set)

    def _has_item(sym_params: set, item: str) -> bool:
        prefix = item + "_"
        return any(p == item or p.startswith(prefix) for p in sym_params)

    syms_missing_core = [
        sym for sym, sym_params in params_by_sym.items()
        if any(not _has_item(sym_params, li) for li in core_universal)
        or not any(_has_item(sym_params, m) for m in profitability_metrics)
    ]
    v.check("financials_core_line_items", len(syms_missing_core) == 0, warn_cond=len(syms_missing_core) <= 10,
            detail=f"{len(syms_missing_core)} symbols missing core items (full panel)")

    # No future-dated rows
    today_ts = pd.Timestamp(date.today())
    future = (panel["date"] > today_ts).sum()
    v.check("financials_no_future_dates", future == 0, warn_cond=future <= 5, detail=f"{future} future-dated rows")

    # 40+ quarters for long-tenure symbols
    first_dates = meta.set_index("symbol")["first_in_universe_date"]
    cutoff = pd.Timestamp(date.today() - timedelta(days=365 * 10))
    long_tenure = [
        s for s in panel_syms
        if s in first_dates.index and pd.notna(first_dates[s]) and pd.Timestamp(first_dates[s]) <= cutoff
    ]
    if long_tenure:
        quarters_per_sym = panel[panel["symbol"].isin(long_tenure)].groupby("symbol")["date"].nunique()
        covered_40q = (quarters_per_sym >= 40).sum()
        pct_40q = 100 * covered_40q / len(long_tenure)
        v.check("financials_quarter_coverage_40q", pct_40q >= 80, warn_cond=pct_40q >= 60,
                detail=f"{pct_40q:.1f}% of long-tenure symbols have >=40 quarters")


# ---------------------------------------------------------------------------
# Corporate-action / survivorship-bias checks (Step 5.5)
# ---------------------------------------------------------------------------

def _load_corporate_actions():
    ca_path = path("corporate_actions.csv")
    if not os.path.exists(ca_path):
        return None
    ca = pd.read_csv(ca_path, parse_dates=["event_date"])
    # Normalize pipe-separated lists into sets per row for easy membership testing.
    ca["_predecessors"] = ca["predecessor_symbols"].apply(
        lambda v: set(str(v).split("|")) if pd.notna(v) else set()
    )
    ca["_successors"] = ca["successor_symbols"].apply(
        lambda v: set(str(v).split("|")) if pd.notna(v) else set()
    )
    return ca


def check_survivorship_bias(v: Validator):
    uh_path = path("universe_history.csv")
    sym_hist_path = path("symbol_history.csv")

    uh = load_csv_optional(uh_path, parse_dates=["quarter_end_date"])
    sym_hist = load_csv_optional(sym_hist_path, parse_dates=["first_seen_date", "last_seen_date"])
    ca = _load_corporate_actions()

    if uh is None:
        v.record("survivorship_bias_exits", WARN, "universe_history.csv missing")
        return

    all_quarters = sorted(uh["quarter_end_date"].unique())
    if len(all_quarters) < 2:
        v.record("survivorship_bias_exits", WARN, "Too few quarters to check")
        return

    latest_q = pd.Timestamp(max(all_quarters))
    # Buffer: allow 1 quarter lag before calling something an "unexplained exit".
    one_q_back = latest_q - pd.DateOffset(months=3)

    # --- Check 1: unexplained exits ---
    # Only flag exits that look like real delistings. An exit is suspicious only when the
    # symbol's status is `delisted_or_renamed` AND its last bhavcopy appearance is within
    # ~12 months of the universe exit (i.e. exit was driven by disappearance, not by mcap
    # dropping below #500). Companies that simply fell out of the ranking are not
    # survivorship gaps and don't need corporate_actions.csv entries.
    sh_by_sym = sym_hist.set_index("symbol") if sym_hist is not None else None
    unexplained_exits = []
    for sym, grp in uh.groupby("symbol"):
        last_q = grp["quarter_end_date"].max()
        if last_q >= one_q_back:
            continue  # still in universe recently
        if ca is not None:
            window_lo = last_q - pd.DateOffset(months=12)
            window_hi = last_q + pd.DateOffset(months=12)
            matched = ca[
                ca["_predecessors"].apply(lambda s: sym in s) &
                (ca["event_date"] >= window_lo) &
                (ca["event_date"] <= window_hi)
            ]
            if not matched.empty:
                continue
        # Ranking churn filter: only flag actual delistings.
        if sh_by_sym is not None and sym in sh_by_sym.index:
            row = sh_by_sym.loc[sym]
            status = row.get("status", "")
            last_seen = row.get("last_seen_date")
            if status != "delisted_or_renamed":
                continue  # still active per bhavcopy → just ranking churn
            if pd.isna(last_seen):
                continue  # last_seen blank means still in most-recent bhavcopy → not a delisting
            if abs((pd.Timestamp(last_seen) - last_q).days) > 365:
                continue  # delisting not contemporaneous with universe exit
        unexplained_exits.append((sym, last_q.date()))

    if unexplained_exits:
        for sym, lq in unexplained_exits[:5]:
            logger.warning(
                f"SURVIVORSHIP: {sym} exited universe at {lq} with no corporate action recorded. "
                "Add row to corporate_actions.csv or update symbol_history.csv status."
            )
    v.check(
        "survivorship_bias_exits",
        len(unexplained_exits) == 0,
        warn_cond=len(unexplained_exits) <= 10,
        detail=f"{len(unexplained_exits)} unexplained exits" +
               (f": {[s for s,_ in unexplained_exits[:3]]}" if unexplained_exits else ""),
    )

    # --- Check 2: unexplained entrances ---
    # Only flag entries that look like genuine survivorship gaps. A late entry is
    # suspicious only when the symbol existed in bhavcopies BEFORE PROJECT_START_DATE
    # (i.e. company was tradeable from day-one but we missed it) AND no corp action
    # explains it. Companies that grew into top 500 over time (ranking churn) and
    # post-PROJECT_START IPOs are normal — not survivorship.
    project_start = pd.Timestamp(config.PROJECT_START_DATE)
    first_q_map = uh.groupby("symbol")["quarter_end_date"].min()
    unexplained_entries = []
    for sym, first_q in first_q_map.items():
        if first_q <= project_start + pd.DateOffset(months=3):
            continue  # present near project start — expected
        if ca is not None:
            window_lo = first_q - pd.DateOffset(months=12)
            window_hi = first_q + pd.DateOffset(months=12)
            ca_match = ca[
                ca["_successors"].apply(lambda s: sym in s) &
                (ca["event_date"] >= window_lo) &
                (ca["event_date"] <= window_hi)
            ]
            if not ca_match.empty:
                continue  # explained by corporate action
        # Genuine survivorship gap: first_seen before project_start (already tradeable
        # at day-one but we somehow added it late). Other late entries (post-start IPOs,
        # slow ranking growth) are not survivorship issues.
        if sh_by_sym is not None and sym in sh_by_sym.index:
            fsd = sh_by_sym.loc[sym].get("first_seen_date")
            if pd.notna(fsd) and pd.Timestamp(fsd) > project_start - pd.DateOffset(months=3):
                continue  # listed after project start or near it → IPO / ranking growth
        unexplained_entries.append((sym, first_q.date()))

    # Entries are informational: we cannot reliably distinguish "company existed pre-project-start
    # but only grew into top 500 later" (normal ranking growth) from "true survivorship gap
    # where a tradeable top-500 company was missed at project start" without historical mcap data.
    # Demoted to WARN: flag for human review but don't fail the pipeline.
    v.check(
        "survivorship_bias_entries",
        len(unexplained_entries) == 0,
        warn_cond=True,
        detail=f"{len(unexplained_entries)} late entries with first_seen pre-project-start" +
               (f" (likely ranking growth): {[s for s,_ in unexplained_entries[:3]]}" if unexplained_entries else ""),
    )


# ---------------------------------------------------------------------------
# Cross-panel checks
# ---------------------------------------------------------------------------

def check_cross_panel(v: Validator):
    meta = load_csv_optional(path("metadata.csv"))
    prices = read_long_panel(path("prices_panel.csv"))
    financials = read_long_panel(path("financials_panel.csv"))
    uh = load_csv_optional(path("universe_history.csv"), parse_dates=["quarter_end_date"])

    if meta is None:
        return
    meta_syms = set(meta["symbol"].dropna().unique())

    if prices is not None and not prices.empty:
        price_syms = set(prices["symbol"].unique())
        orphan = price_syms - meta_syms
        v.check("cross_prices_in_metadata", len(orphan) == 0, warn_cond=len(orphan) <= 5,
                detail=f"{len(orphan)} price symbols not in metadata")

    if financials is not None and not financials.empty:
        fin_syms = set(financials["symbol"].unique())
        orphan = fin_syms - meta_syms
        v.check("cross_financials_in_metadata", len(orphan) == 0, warn_cond=len(orphan) <= 5,
                detail=f"{len(orphan)} financial symbols not in metadata")

    if uh is not None and prices is not None and financials is not None:
        uh_syms = set(uh["symbol"].unique())
        price_syms = set(prices["symbol"].unique())
        fin_syms = set(financials["symbol"].unique())
        critical_missing = uh_syms - price_syms - fin_syms
        v.check("cross_universe_in_both_panels", len(critical_missing) == 0, warn_cond=len(critical_missing) <= 10,
                detail=f"{len(critical_missing)} universe symbols in NEITHER panel")


# ---------------------------------------------------------------------------
# Failed-log checks
# ---------------------------------------------------------------------------

def check_failed_logs(v: Validator):
    meta = load_csv_optional(path("metadata.csv"))
    n_meta = len(meta) if meta is not None else 1

    for label, script, threshold in [
        ("failed_financials", "fetch_financials", 0.15),
    ]:
        fp = _load_script_gaps(script)
        if fp is None or fp.empty:
            v.record(f"{label}_ratio", PASS, "No failures")
            continue
        n_failed = fp["symbol"].nunique()
        ratio = n_failed / max(n_meta, 1)
        v.check(f"{label}_ratio", ratio < threshold, warn_cond=ratio < threshold * 2,
                detail=f"{ratio*100:.1f}% ({n_failed}/{n_meta}); threshold {threshold*100:.0f}%")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info("=== Running validation checks ===")

    v = Validator()
    check_shares_outstanding(v)
    check_universe(v)
    check_survivorship_bias(v)
    check_prices(v)
    check_financials(v)
    check_cross_panel(v)
    check_failed_logs(v)

    results = v.results
    n_fail = sum(1 for r in results if r.status == FAIL)
    n_warn = sum(1 for r in results if r.status == WARN)
    n_pass = sum(1 for r in results if r.status == PASS)

    header = f"\n{'CHECK':<48} {'STATUS':<6} {'DETAIL'}"
    sep = "-" * 95
    lines = [header, sep] + [str(r) for r in results] + [sep]
    overall = "PASS" if n_fail == 0 else "FAIL"
    summary = f"OVERALL: {overall} ({n_warn} warnings, {n_fail} failures, {n_pass} passed)"
    lines.append(summary)

    report = "\n".join(lines)
    print(report)

    os.makedirs(config.DATA_DIR, exist_ok=True)
    report_path = path("validation_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info(f"Validation report written -> {report_path}")

    if n_fail > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
