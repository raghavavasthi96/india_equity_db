"""
universe.py — Build point-in-time top-500 universe, quarterly rebalanced.

Shares come from XBRL filings (fetch_shp_xbrl.py) and are already point-in-time;
no corp-action adjustment is performed here.

Run order:
    python universe.py --bootstrap [--refresh-universe]   # writes flat metadata.csv
    python fetch_shp_xbrl.py                              # writes shares_outstanding.csv
    python universe.py [--refresh-universe]               # ranks and writes universe_history.csv
"""

import argparse
import glob
import os
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from tqdm import tqdm

from pipeline.configs import config
from .utils.logger import get_logger
from .utils.io import read_csv_if_exists, path, read_long_panel, write_long_panel
from .utils.bhavcopy import fetch_bhavcopy
from .utils.http import make_session
from .utils.cli import parse_symbol_list
from .utils.dates import enforce_project_floor, quarter_end_dates

logger = get_logger("universe")

NSE_EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


# EQUITY_L.csv schema as of 2026. Fail loud if NSE renames any of these.
_NSE_EQ_REQUIRED = {"symbol", "name_of_company", "series", "date_of_listing", "isin_number"}


def fetch_nse_equity_list() -> pd.DataFrame:
    logger.info("Fetching NSE equity list ...")
    session = make_session(warmup=False)
    headers = {
        "Referer": "https://www.nseindia.com/",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = session.get(NSE_EQUITY_LIST_URL, headers=headers, timeout=30)
    r.raise_for_status()

    from io import StringIO
    df = pd.read_csv(StringIO(r.text))
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    missing = _NSE_EQ_REQUIRED - set(df.columns)
    if missing:
        raise KeyError(
            f"NSE EQUITY_L.csv schema changed; missing columns {missing}. "
            f"Got: {list(df.columns)}"
        )

    df = df[df["series"].str.strip() == "EQ"]

    result = pd.DataFrame({
        "symbol": df["symbol"].str.strip(),
        "isin": df["isin_number"].str.strip(),
        "company_name": df["name_of_company"].str.strip(),
        "listing_date": pd.to_datetime(df["date_of_listing"], errors="coerce"),
        "delisting_date": pd.NaT,
        "status": "active",
    })

    result = result.drop_duplicates(subset=["symbol"]).reset_index(drop=True)
    logger.info(f"NSE equity list: {len(result)} active EQ symbols")
    return result


def fetch_wikipedia_supplement() -> pd.DataFrame:
    import requests as req
    from bs4 import BeautifulSoup

    logger.info("Fetching Wikipedia NSE supplement ...")
    url = "https://en.wikipedia.org/wiki/List_of_companies_listed_on_the_National_Stock_Exchange_of_India"
    rows = []
    try:
        r = req.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "lxml")
        for table in soup.find_all("table", class_="wikitable"):
            for tr in table.find_all("tr")[1:]:
                tds = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if len(tds) >= 2:
                    rows.append({"symbol": tds[0], "company_name": tds[1]})
    except Exception as e:
        logger.warning(f"Wikipedia fetch failed (non-fatal): {e}")
        return pd.DataFrame(columns=["symbol", "isin", "company_name", "listing_date", "delisting_date", "status"])

    df = pd.DataFrame(rows)
    df["isin"] = pd.NA
    df["listing_date"] = pd.NaT
    df["delisting_date"] = pd.NaT
    df["status"] = "unknown"
    df.drop_duplicates(subset=["symbol"], inplace=True)
    logger.info(f"Wikipedia supplement: {len(df)} symbols")
    return df


def _fetch_bhavcopy_near(q: date, session, max_offset: int = 7) -> Optional[pd.DataFrame]:
    """
    Try q, q-1, ..., q-max_offset. Returns the first bhavcopy DataFrame found, or None.
    Already-cached candidates short-circuit the network call.
    """
    for offset in range(max_offset + 1):
        candidate = q - timedelta(days=offset)
        try:
            return fetch_bhavcopy(candidate, session=session)
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning(f"bhavcopy error for {candidate}: {e}")
            continue
    return None


def _warm_bhavcopy_cache() -> None:
    """
    Fetch quarter-end bhavcopies across PROJECT_START_DATE..today and cache them.
    No-op for quarter ends already cached.
    """
    project_start = date.fromisoformat(config.PROJECT_START_DATE)
    q_dates = quarter_end_dates(project_start, date.today())
    logger.info(f"Warming bhavcopy_cache: {len(q_dates)} quarter ends to check")
    session = make_session()
    for q in tqdm(q_dates, desc="Warming bhavcopy cache"):
        _fetch_bhavcopy_near(q, session=session)


def build_symbol_history_from_bhavcopies() -> pd.DataFrame:
    """
    Scan bhavcopy_cache/*.parquet; compute first/last seen dates per symbol.
    Merges ISIN + status from live NSE EQUITY_L. Writes data/symbol_history.csv.
    If the bhavcopy cache is empty, warms it first by fetching quarter-end snapshots
    from PROJECT_START_DATE..today (removes circular dependency with universe ranking).
    """
    parquet_files = sorted(glob.glob(os.path.join(config.BHAVCOPY_CACHE_DIR, "*.parquet")))
    if not parquet_files:
        logger.warning("bhavcopy_cache empty; warming before building symbol_history.")
        _warm_bhavcopy_cache()
        parquet_files = sorted(glob.glob(os.path.join(config.BHAVCOPY_CACHE_DIR, "*.parquet")))

    if not parquet_files:
        logger.warning("No bhavcopy parquets after warm attempt; symbol_history will be empty.")
        return pd.DataFrame(columns=["symbol", "first_seen_date", "last_seen_date", "status", "isin", "source"])

    frames = []
    for f in parquet_files:
        try:
            df = pd.read_parquet(f, columns=["symbol", "date"])
            frames.append(df)
        except Exception as e:
            logger.warning(f"Could not read {f}: {e}")

    if not frames:
        return pd.DataFrame(columns=["symbol", "first_seen_date", "last_seen_date", "status", "isin", "source"])

    all_data = pd.concat(frames, ignore_index=True)
    all_data["date"] = pd.to_datetime(all_data["date"])

    grp = all_data.groupby("symbol")["date"].agg(["min", "max"]).reset_index()
    grp.columns = ["symbol", "first_seen_date", "last_seen_date"]

    most_recent_date = all_data["date"].max()
    in_latest = set(all_data[all_data["date"] == most_recent_date]["symbol"].unique())
    # Symbols still present in the most recent parquet have no known last_seen_date.
    grp.loc[grp["symbol"].isin(in_latest), "last_seen_date"] = pd.NaT

    try:
        nse_df = fetch_nse_equity_list()
        isin_map = nse_df.set_index("symbol")["isin"].to_dict()
        active_symbols = set(nse_df["symbol"].tolist())
    except Exception as e:
        logger.warning(f"NSE equity list fetch failed during symbol_history build: {e}")
        isin_map = {}
        active_symbols = set()

    grp["isin"] = grp["symbol"].map(isin_map)
    grp["status"] = grp["symbol"].apply(
        lambda s: "active" if s in active_symbols else "delisted_or_renamed"
    )
    grp["source"] = "bhavcopy_backfill"

    os.makedirs(config.DATA_DIR, exist_ok=True)
    out_path = path("symbol_history.csv")
    grp.to_csv(out_path, index=False)
    logger.info(f"symbol_history saved: {len(grp)} symbols -> {out_path}")
    return grp


def build_raw_universe(force: bool = False) -> pd.DataFrame:
    """
    Union-only build: never drops previously-seen symbols.
    Sources (priority order): live NSE EQUITY_L > existing raw_universe > Wikipedia > symbol_history.
    --refresh-universe (force=True) triggers re-fetch and re-union; idempotent on re-run.
    """
    raw_path = path("raw_universe.csv")

    if os.path.exists(raw_path) and not force:
        logger.info(f"Loading cached raw_universe from {raw_path}")
        return pd.read_csv(raw_path, parse_dates=["listing_date", "delisting_date"])

    # Load existing as union baseline so previously-seen symbols are never dropped.
    existing: Optional[pd.DataFrame] = None
    if os.path.exists(raw_path):
        try:
            existing = pd.read_csv(raw_path, parse_dates=["listing_date", "delisting_date"])
            logger.info(f"Existing raw_universe: {len(existing)} symbols (union baseline)")
        except Exception as e:
            logger.warning(f"Could not load existing raw_universe: {e}")

    nse_df = fetch_nse_equity_list()
    wiki_df = fetch_wikipedia_supplement()

    # Build supplement from symbol_history (historical symbols from bhavcopies).
    sym_hist_path = path("symbol_history.csv")
    hist_cols = ["symbol", "isin", "company_name", "listing_date", "delisting_date", "status"]
    hist_supplement = pd.DataFrame(columns=hist_cols)
    if os.path.exists(sym_hist_path):
        try:
            sym_hist = pd.read_csv(sym_hist_path)
            hist_supplement = pd.DataFrame({
                "symbol": sym_hist["symbol"],
                "isin": sym_hist.get("isin", pd.NA),
                "company_name": pd.NA,
                "listing_date": pd.NaT,
                "delisting_date": pd.NaT,
                "status": sym_hist.get("status", "delisted_or_renamed"),
            })
            logger.info(f"symbol_history supplement: {len(hist_supplement)} symbols")
        except Exception as e:
            logger.warning(f"Could not load symbol_history: {e}")

    # Union: NSE live list takes priority (first-occurrence wins in dedup).
    parts = [nse_df]
    if existing is not None:
        parts.append(existing)
    parts.extend([wiki_df, hist_supplement])

    combined = pd.concat(parts, ignore_index=True)
    combined.drop_duplicates(subset=["symbol"], keep="first", inplace=True)

    # Recompute status and ISIN from live NSE (authoritative).
    active_symbols = set(nse_df["symbol"])
    isin_live = nse_df.set_index("symbol")["isin"]

    is_active = combined["symbol"].isin(active_symbols)
    combined["status"] = is_active.map({True: "active", False: "delisted_or_renamed"})
    combined["isin"] = combined["symbol"].map(isin_live).fillna(combined["isin"])

    combined = combined.sort_values("symbol").reset_index(drop=True)

    os.makedirs(config.DATA_DIR, exist_ok=True)
    combined.to_csv(raw_path, index=False)
    logger.info(f"raw_universe saved: {len(combined)} symbols -> {raw_path}")
    return combined


def compute_universe_history(
    raw_universe: pd.DataFrame,
    shares_df: pd.DataFrame,
    start_date: Optional[date] = None,
) -> pd.DataFrame:
    today = date.today()
    if start_date is None:
        start_date = date.fromisoformat(config.PROJECT_START_DATE)
    enforce_project_floor(start_date)
    q_dates = quarter_end_dates(start_date, today)

    symbols = raw_universe["symbol"].tolist()
    isin_map = raw_universe.set_index("symbol")["isin"].to_dict()

    # XBRL-sourced shares are pre-adjusted point-in-time; no corp-action math needed.
    shares_by_symbol = {}
    if shares_df is not None and not shares_df.empty:
        shares_df = shares_df.copy()
        shares_df["date"] = pd.to_datetime(shares_df["date"])
        for sym, grp in shares_df.groupby("symbol"):
            shares_by_symbol[sym] = grp.sort_values("date").reset_index(drop=True)

    archive_session = make_session()

    rows = []
    for q in tqdm(q_dates, desc="Ranking quarters"):
        q_ts = pd.Timestamp(q)

        bhavcopy_df = _fetch_bhavcopy_near(q, session=archive_session)
        if bhavcopy_df is None or bhavcopy_df.empty:
            logger.warning(f"No bhavcopy within 7 days of {q}, skipping quarter")
            continue

        close_map = bhavcopy_df.set_index("symbol")["close"].to_dict()

        mcap_entries = []
        for sym in symbols:
            close = close_map.get(sym)
            if close is None:
                continue
            try:
                close = float(close)
            except (ValueError, TypeError):
                continue

            sdf = shares_by_symbol.get(sym)
            if sdf is None:
                continue
            # Strict point-in-time: only filings on or before quarter end.
            # Earlier code used a +90 day forward fallback, which leaked future data
            # into the ranking and biased backtests.
            mask = sdf["date"] <= q_ts
            if not mask.any():
                continue
            row = sdf[mask].iloc[-1]

            shares = float(row["total_shares"])
            free_float = float(row["free_float_shares"])

            mcap_entries.append({
                "symbol": sym,
                "close_inr": close,
                "shares_outstanding": shares,
                "free_float_shares": free_float,
                "market_cap_inr": close * shares,
                "free_float_market_cap_inr": close * free_float,
            })

        if not mcap_entries:
            continue

        q_df = (
            pd.DataFrame(mcap_entries)
            .sort_values("market_cap_inr", ascending=False)
            .reset_index(drop=True)
        )
        top500 = q_df.head(config.UNIVERSE_SIZE).copy()
        top500.insert(0, "quarter_end_date", q)
        top500.insert(1, "rank", range(1, len(top500) + 1))
        top500["isin"] = top500["symbol"].map(isin_map)

        rows.append(top500[[
            "quarter_end_date", "rank", "symbol", "isin",
            "market_cap_inr", "free_float_market_cap_inr",
            "close_inr", "shares_outstanding", "free_float_shares",
        ]])

    if not rows:
        return pd.DataFrame()

    universe_hist = pd.concat(rows, ignore_index=True)
    out_path = path("universe_history.csv")
    universe_hist.to_csv(out_path, index=False)
    logger.info(f"universe_history saved: {len(universe_hist)} rows -> {out_path}")
    return universe_hist


_METADATA_COLS = [
    "symbol", "isin", "company_name", "sector", "industry",
    "first_in_universe_date", "last_in_universe_date", "status",
]


def _finalize_metadata(meta: pd.DataFrame, label: str) -> pd.DataFrame:
    """Pad missing canonical columns with NA, reorder, and write metadata.csv."""
    for c in _METADATA_COLS:
        if c not in meta.columns:
            meta[c] = pd.NA
    meta = meta[_METADATA_COLS]
    out_path = path("metadata.csv")
    meta.to_csv(out_path, index=False)
    logger.info(f"{label}: {len(meta)} symbols -> {out_path}")
    return meta


def build_metadata(raw_universe: pd.DataFrame, universe_hist: pd.DataFrame) -> pd.DataFrame:
    if universe_hist is None or universe_hist.empty:
        logger.warning("universe_history is empty; metadata will be empty.")
        return pd.DataFrame()

    symbols_in_universe = universe_hist["symbol"].unique()
    first_dates = universe_hist.groupby("symbol")["quarter_end_date"].min().rename("first_in_universe_date")
    last_dates = universe_hist.groupby("symbol")["quarter_end_date"].max().rename("last_in_universe_date")

    # Left-join from universe symbols so delisted tickers missing from current raw_universe are preserved.
    raw_cols = [c for c in ["symbol", "isin", "company_name", "listing_date", "delisting_date", "status"]
                if c in raw_universe.columns]
    meta = (
        pd.DataFrame({"symbol": symbols_in_universe})
        .merge(raw_universe[raw_cols], on="symbol", how="left")
        .merge(first_dates, on="symbol", how="left")
        .merge(last_dates, on="symbol", how="left")
    )
    meta["sector"] = pd.NA
    meta["industry"] = pd.NA

    meta = _finalize_metadata(meta, label="metadata saved")
    _prune_panels_to_metadata(set(meta["symbol"].dropna()))
    return meta


def _prune_panels_to_metadata(meta_syms: set) -> None:
    for panel_name in ("financials_panel.csv", "financials_annual_panel.csv"):
        panel_path = path(panel_name)
        panel = read_long_panel(panel_path)
        if panel is None or panel.empty:
            continue
        orphan_syms = set(panel["symbol"].unique()) - meta_syms
        if orphan_syms:
            n_before = len(panel)
            panel = panel[panel["symbol"].isin(meta_syms)]
            write_long_panel(panel, panel_path)
            logger.info(
                f"{panel_name}: removed {n_before - len(panel)} rows "
                f"for {len(orphan_syms)} non-universe symbols"
            )


def _write_bootstrap_metadata(raw_universe: pd.DataFrame):
    meta = raw_universe.copy()
    meta["sector"] = pd.NA
    meta["industry"] = pd.NA
    meta["first_in_universe_date"] = pd.NA
    meta["last_in_universe_date"] = pd.NA
    _finalize_metadata(meta, label="Bootstrap metadata saved")


def main():
    parser = argparse.ArgumentParser(description="Build point-in-time NSE top-500 universe.")
    parser.add_argument("--bootstrap", action="store_true",
                        help="Write flat metadata.csv with all symbols (no ranking). Run before fetch_financials.py.")
    parser.add_argument("--refresh-universe", action="store_true",
                        help="Force re-download of raw universe")
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated symbol subset")
    parser.add_argument("--start-date", type=str, default=None,
                        help=f"Start date YYYY-MM-DD for universe history (>= {config.PROJECT_START_DATE})")
    args = parser.parse_args()

    os.makedirs(config.DATA_DIR, exist_ok=True)

    if args.refresh_universe:
        build_symbol_history_from_bhavcopies()
    raw_universe = build_raw_universe(force=args.refresh_universe)

    subset = parse_symbol_list(args.symbols)
    if subset:
        raw_universe = raw_universe[raw_universe["symbol"].isin(subset)].copy()
        logger.info(f"Symbol subset: {subset}")

    if args.bootstrap:
        _write_bootstrap_metadata(raw_universe)
        logger.info("Bootstrap complete. Run fetch_financials.py next.")
        return

    shares_path = path("shares_outstanding.csv")
    shares_df = read_csv_if_exists(shares_path, parse_dates=["date"])

    if args.start_date:
        start_date = enforce_project_floor(date.fromisoformat(args.start_date))
    else:
        start_date = date.fromisoformat(config.PROJECT_START_DATE)
    universe_hist = compute_universe_history(raw_universe, shares_df, start_date)

    build_metadata(raw_universe, universe_hist)
    logger.info("Universe build complete.")


if __name__ == "__main__":
    main()
