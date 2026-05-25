"""
fetch_prices.py — Fetch adjusted OHLCV from yfinance, with NSE bhavcopy fallback.

Primary source: yfinance (auto_adjust=True, batch).
Fallback: for symbols yfinance returns empty (typically merged/delisted, e.g. HDFC,
TATAMOTORS), iterate daily NSE bhavcopies and back-adjust using NSE corporate actions.

Outputs:
  data/prices_panel.csv — long format (date, symbol, parameter, value)
  data/corporate_action_adjustments.csv — flattened corp action ledger for fallback symbols
  data/data_gaps.csv — appended for symbols where fallback fails (script=fetch_prices)

Run:
    python fetch_prices.py [--symbols A,B,C] [--start YYYY-MM-DD] [--no-fallback]
"""

import argparse
import os
from datetime import date, timedelta
from typing import List, Optional

import pandas as pd
import yfinance as yf
from tqdm import tqdm

from pipeline.configs import config
from .utils.bhavcopy import fetch_bhavcopy
from .utils.cli import parse_symbol_list
from .utils.dates import enforce_price_history_floor, price_history_floor
from .utils.http import make_session
from .utils.gaps import log_gap
from .utils.io import path, wide_to_long, write_long_panel
from .utils.logger import get_logger
from .utils.nse_corpact import get_adjustments
from .utils.price_adjust import apply_adjustments

logger = get_logger("fetch_prices")

FIELDS = ["Open", "High", "Low", "Close", "Volume"]


def _slice_yf_for_symbol(prices_raw: pd.DataFrame, sym: str) -> Optional[pd.DataFrame]:
    """
    Extract one symbol's OHLCV slice from a yf.download() result and return long-format.
    Returns None on any failure (missing ticker, all-NaN, conversion error) — caller routes
    these symbols to the bhavcopy fallback.
    """
    try:
        if isinstance(prices_raw.columns, pd.MultiIndex):
            sym_ticker = f"{sym}.NS"
            if sym_ticker not in prices_raw.columns.get_level_values(0):
                return None
            df = prices_raw[sym_ticker].copy()
        else:
            df = prices_raw.copy()

        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        available = [f for f in FIELDS if f in df.columns]
        df = df[available]
        df.columns = [c.lower() for c in df.columns]
        df = df.dropna(how="all")
        if df.empty:
            return None

        long_df = wide_to_long(df, sym)
        if long_df is None or long_df.empty:
            return None
        return long_df
    except Exception as e:
        logger.error(f"{sym}: yfinance conversion failed - {e}")
        return None


def _symbol_date_range(symbol: str, default_start: date, today: date) -> tuple:
    """Return (start, end) covering symbol's known life from symbol_history.csv.

    Pulls `PRICE_HISTORY_LOOKBACK_DAYS` of prior history before the symbol's
    first appearance so signal lookbacks (e.g. momentum_12_1) have a runway at
    each symbol's first eligibility date. `default_start` already floors at
    `PROJECT_START_DATE − lookback`, so symbols present from day-one get the
    full buffer too.
    """
    sym_hist_path = path("symbol_history.csv")
    start, end = default_start, today
    lookback = timedelta(days=config.PRICE_HISTORY_LOOKBACK_DAYS)
    if os.path.exists(sym_hist_path):
        sh = pd.read_csv(sym_hist_path, parse_dates=["first_seen_date", "last_seen_date"])
        row = sh[sh["symbol"] == symbol]
        if not row.empty:
            r = row.iloc[0]
            if pd.notna(r["first_seen_date"]):
                start = max(default_start, r["first_seen_date"].date() - lookback)
            if pd.notna(r["last_seen_date"]):
                end = min(today, r["last_seen_date"].date())
    return start, end


def _build_fallback_panels(symbols: List[str], default_start: date, today: date,
                           nse_session) -> dict:
    """One-pass scan of bhavcopies: returns dict[symbol -> wide OHLCV df].

    Loads each daily bhavcopy once and dispatches rows to all fallback symbols
    in its EQ panel, honoring per-symbol [start, end] windows from symbol_history.
    """
    windows = {s: _symbol_date_range(s, default_start, today) for s in symbols}
    if not windows:
        return {}

    sym_set = set(symbols)
    union_start = min(w[0] for w in windows.values())
    union_end = max(w[1] for w in windows.values())
    logger.info(f"Bhavcopy scan union range: {union_start} → {union_end}")

    chunks = []
    days = (union_end - union_start).days + 1
    for offset in tqdm(range(days), desc="Bhavcopy scan"):
        d = union_start + timedelta(days=offset)
        if d.weekday() >= 5:
            continue
        try:
            bdf = fetch_bhavcopy(d, session=nse_session)
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.debug(f"bhavcopy {d} error - {e}")
            continue
        sub = bdf[bdf["symbol"].isin(sym_set)][
            ["symbol", "open", "high", "low", "close", "volume"]
        ].copy()
        if sub.empty:
            continue
        sub["date"] = pd.Timestamp(d)
        chunks.append(sub)

    if not chunks:
        return {}

    big = pd.concat(chunks, ignore_index=True)
    panels = {}
    for sym, group in big.groupby("symbol", sort=False):
        start, end = windows[sym]
        g = group[(group["date"] >= pd.Timestamp(start)) & (group["date"] <= pd.Timestamp(end))]
        if g.empty:
            continue
        panels[sym] = g.drop(columns="symbol").set_index("date").sort_index()
    return panels


def _adjust_fallback_panel(symbol: str, raw_df: pd.DataFrame, nse_session,
                           all_adjustments: list) -> pd.DataFrame:
    """Apply corp-action adjustments to a per-symbol bhavcopy panel and return long-format."""
    actions_df = get_adjustments(symbol, session=nse_session)
    if not actions_df.empty:
        all_adjustments.append(actions_df)
        logger.info(f"{symbol}: applying {len(actions_df)} corp actions")
        adjusted = apply_adjustments(raw_df, actions_df, symbol=symbol)
    else:
        logger.warning(f"{symbol}: no NSE corp actions found; using unadjusted bhavcopy prices")
        log_gap("fetch_prices", symbol, "no_corp_actions_for_fallback")
        adjusted = raw_df

    adjusted.columns = [c.lower() for c in adjusted.columns]
    return wide_to_long(adjusted, symbol)


def main():
    parser = argparse.ArgumentParser(description="Fetch adjusted OHLCV prices for NSE top-500 universe.")
    parser.add_argument("--symbols", type=str, default=None)
    parser.add_argument("--start-date", type=str, default=None,
                        help=f"Fetch start date YYYY-MM-DD. Lower bound: "
                             f"PROJECT_START_DATE ({config.PROJECT_START_DATE}) "
                             f"minus PRICE_HISTORY_LOOKBACK_DAYS "
                             f"({config.PRICE_HISTORY_LOOKBACK_DAYS}).")
    parser.add_argument("--no-fallback", action="store_true",
                        help="Skip bhavcopy fallback for symbols yfinance returns empty (yfinance-only)")
    args = parser.parse_args()

    os.makedirs(config.DATA_DIR, exist_ok=True)

    metadata_path = path("metadata.csv")
    if not os.path.exists(metadata_path):
        logger.error(f"metadata.csv not found at {metadata_path}. Run universe.py first.")
        raise SystemExit(1)

    metadata = pd.read_csv(metadata_path)
    symbols: List[str] = metadata["symbol"].dropna().unique().tolist()

    subset = parse_symbol_list(args.symbols)
    if subset:
        symbols = [s for s in symbols if s in subset]
        logger.info(f"Symbol subset: {symbols}")

    today = date.today()
    today_str = str(today)
    default_start = price_history_floor()
    lookback = timedelta(days=config.PRICE_HISTORY_LOOKBACK_DAYS)

    if args.start_date:
        start_str = str(enforce_price_history_floor(date.fromisoformat(args.start_date)))
    else:
        sym_meta = metadata[metadata["symbol"].isin(symbols)]
        listing_dates = pd.to_datetime(sym_meta["first_in_universe_date"], errors="coerce").dropna()
        per_symbol_starts = [max(d.date() - lookback, default_start) for d in listing_dates]
        start_str = str(min(per_symbol_starts)) if per_symbol_starts else str(default_start)

    logger.info(f"Downloading {len(symbols)} symbols from {start_str} to {today_str} ...")

    tickers_ns = [f"{s}.NS" for s in symbols]
    try:
        prices_raw = yf.download(
            tickers_ns,
            start=start_str,
            end=today_str,
            auto_adjust=True,
            actions=False,
            group_by="ticker",
            threads=True,
            progress=True,
        )
    except Exception as e:
        logger.error(f"Batch download failed: {e}")
        raise SystemExit(1)

    if prices_raw.empty:
        logger.warning("yfinance returned no data.")
        prices_raw = pd.DataFrame()

    long_frames: List[pd.DataFrame] = []
    fallback_symbols: List[str] = []

    for sym in tqdm(symbols, desc="Converting to long format"):
        long_df = _slice_yf_for_symbol(prices_raw, sym)
        if long_df is None:
            fallback_symbols.append(sym)
            continue
        long_frames.append(long_df)
        logger.debug(f"{sym}: {len(long_df)} rows (yfinance)")

    if fallback_symbols and not args.no_fallback:
        logger.info(f"Bhavcopy fallback needed for {len(fallback_symbols)} symbols: {fallback_symbols[:10]}{'...' if len(fallback_symbols) > 10 else ''}")
        nse_session = make_session()
        all_adjustments: List[pd.DataFrame] = []

        panels = _build_fallback_panels(fallback_symbols, default_start, today, nse_session)

        for sym in tqdm(fallback_symbols, desc="Adjust fallback"):
            raw_df = panels.get(sym)
            if raw_df is None or raw_df.empty:
                logger.warning(f"{sym}: bhavcopy returned no rows in range")
                log_gap("fetch_prices", sym, "bhavcopy_no_rows_in_range")
                continue
            try:
                long_df = _adjust_fallback_panel(sym, raw_df, nse_session, all_adjustments)
                if long_df is not None and not long_df.empty:
                    long_frames.append(long_df)
                    logger.info(f"{sym}: {len(long_df)} rows (bhavcopy fallback)")
            except Exception as e:
                logger.error(f"{sym}: fallback failed - {e}")

        if all_adjustments:
            adjustments_df = pd.concat(all_adjustments, ignore_index=True)
            adj_path = path("corporate_action_adjustments.csv")
            adjustments_df.to_csv(adj_path, index=False)
            logger.info(f"corporate_action_adjustments saved: {len(adjustments_df)} rows -> {adj_path}")
    elif fallback_symbols and args.no_fallback:
        logger.warning(f"--no-fallback set; skipping {len(fallback_symbols)} symbols with no yfinance data")

    if not long_frames:
        logger.warning("No data to write.")
        raise SystemExit(0)

    panel = pd.concat(long_frames, ignore_index=True)
    prices_path = path("prices_panel.csv")
    write_long_panel(panel, prices_path)
    logger.info(f"prices_panel saved: {len(panel)} rows -> {prices_path}")
    logger.info("fetch_prices complete.")


if __name__ == "__main__":
    main()
