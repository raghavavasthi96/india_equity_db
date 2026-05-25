"""
fetch_financials.py — Script 2: Download consolidated quarterly financials from screener.in.

Parses financial tables from the public HTML company page (no login required).
HTML cached per symbol; re-downloaded when older than SCREENER_CACHE_DAYS.

Output (long format: date, symbol, parameter, value):
  data/financials_panel.csv        — quarterly P&L; dates = quarter-end (Mar31/Jun30/Sep30/Dec31)
  data/financials_annual_panel.csv — annual P&L + BS + CF; dates = 31-Mar-YYYY (Indian FY end)

Canonical parameters — non-financial companies:
  P&L:  total_revenue, operating_expenses, other_income, ebitda, depreciation, ebit,
         interest_expense, pbt, tax_implied(*), net_profit, eps
  BS:   total_assets, net_equity, total_liabilities, total_debt, other_liabilities,
         fixed_assets, cwip, investments, other_assets        [annual only]
  CF:   cfo, cfi, cff, net_cash_flow, fcf                    [annual only]

Canonical parameters — banking / NBFC companies (instead of ebitda/ebit):
  Additional P&L:  financing_profit (NII), financing_margin (NIM %)
  Additional Q:    gross_npa, net_npa                         [NPA quality ratios]
  Additional BS:   deposits                                   [annual only]
  Note: ebitda and ebit are absent for banks — not meaningful concepts for financials.
  Note: total_debt maps from screener's "Borrowings" (non-banks) or "Borrowing" (banks).

(*) tax_implied = pbt - net_profit (approximation; screener shows Tax% which is the rate, not the amount).
    Exact when there are no minority interest adjustments, share of associates, or discontinued ops.

NOT AVAILABLE from screener public HTML: Current Assets, Current Liabilities.

Run:
    python fetch_financials.py [--symbols A,B,C] [--force]
"""

import argparse
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Optional

import pandas as pd
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from tqdm import tqdm
from tenacity import retry, stop_after_attempt, wait_exponential

from pipeline.configs import config
from .utils.logger import get_logger
from .utils.http import make_session, polite_sleep
from .utils.cli import parse_symbol_list
from .utils.dates import parse_quarter_date, parse_annual_date, financials_history_floor
from .utils.io import read_long_panel, write_long_panel, merge_long_panel, wide_to_long, path
from .utils.gaps import log_gap

logger = get_logger("fetch_financials")

SCREENER_BASE = "https://www.screener.in"

QUARTERLY_SECTIONS = ["quarters"]
ANNUAL_SECTIONS    = ["profit-loss", "balance-sheet", "cash-flow"]

# ---------------------------------------------------------------------------
# Parameter normalization
# ---------------------------------------------------------------------------

# Maps screener snake_case column names -> canonical names.
# Prefix "_" = intermediate helper used only for computing derived fields.
PARAM_MAP = {
    # P&L (quarterly & annual)
    "sales":                            "total_revenue",
    "revenue":                          "total_revenue",   # banks: screener uses "Revenue" not "Sales"
    "expenses":                         "operating_expenses",
    "other_income":                     "other_income",
    "operating_profit":                 "ebitda",       # screener: Sales - Expenses (excl D&A, interest)
    "depreciation":                     "depreciation",
    "interest":                         "interest_expense",
    "profit_before_tax":                "pbt",
    "net_profit":                       "net_profit",
    "eps_in_rs":                        "eps",
    # Balance Sheet (annual only)
    "equity_capital":                   "_share_capital",
    "share_capital":                    "_share_capital",
    "reserves":                         "_reserves",
    "reserves_surplus":                 "_reserves",
    "borrowings":                       "total_debt",
    "borrowing":                        "total_debt",      # banks: singular form on screener
    "other_liabilities":                "other_liabilities",
    # screener's "total_liabilities" == total_assets (liability+equity side) — replaced by computed version
    "total_assets":                     "total_assets",
    "fixed_assets":                     "fixed_assets",
    "cwip":                             "cwip",
    "investments":                      "investments",
    "other_assets":                     "other_assets",
    # Cash Flow (annual only)
    "cash_from_operating_activity":     "cfo",
    "cash_from_investing_activity":     "cfi",
    "cash_from_financing_activity":     "cff",
    "net_cash_flow":                    "net_cash_flow",
    "free_cash_flow":                   "fcf",          # screener provides this directly
}

# Screener columns to discard after parsing (ratios, artifacts, or replaced by computed versions)
# - total_liabilities: screener's version = total_assets (misleading); replaced by computed version
# - opm:              operating profit margin % (recomputable)
# - tax:              screener shows Tax Rate % (e.g. 27.0 = 27%); we compute tax_implied = pbt - net_profit
# - cfo_op:           screener ratio artifact (CFO / operating profit %)
# - dividend_payout:  dividend payout ratio % (not in target parameter list)
DROP_RAW = {"total_liabilities", "opm", "tax", "cfo_op", "dividend_payout"}


def normalize_wide(wide: pd.DataFrame, is_annual: bool) -> pd.DataFrame:
    """
    Apply PARAM_MAP renames, drop unwanted columns, and compute derived fields.
    Input:  wide DataFrame — date index, screener snake_case columns.
    Output: wide DataFrame — date index, canonical columns.
    """
    # 1. Rename known columns; unknown columns pass through unchanged
    rename = {c: PARAM_MAP[c] for c in wide.columns if c in PARAM_MAP}
    df = wide.rename(columns=rename)

    # 2. Drop screener artifacts and replaced columns
    df = df.drop(columns=[c for c in DROP_RAW if c in df.columns], errors="ignore")

    # 3. Computed: EBIT = EBITDA - Depreciation
    if "ebitda" in df.columns and "depreciation" in df.columns:
        df["ebit"] = df["ebitda"] - df["depreciation"]

    # 4. Computed: Implied Tax = PBT - Net Profit
    #    Screener's "Tax %" row (dropped above) is the rate, not the amount.
    #    Exact only when there is no minority interest, share of associates, or discontinued ops.
    if "pbt" in df.columns and "net_profit" in df.columns:
        df["tax_implied"] = df["pbt"] - df["net_profit"]

    # 5. Annual-only derived fields
    if is_annual:
        if "_share_capital" in df.columns and "_reserves" in df.columns:
            df["net_equity"] = df["_share_capital"] + df["_reserves"]
        if "total_assets" in df.columns and "net_equity" in df.columns:
            df["total_liabilities"] = df["total_assets"] - df["net_equity"]
        # FCF fallback if screener didn't provide free_cash_flow
        if "fcf" not in df.columns and "cfo" in df.columns and "cfi" in df.columns:
            df["fcf"] = df["cfo"] + df["cfi"]

    # 6. Drop helper columns
    helper_cols = [c for c in df.columns if c.startswith("_")]
    return df.drop(columns=helper_cols)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_tls = threading.local()


def _thread_session() -> cffi_requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = make_session(warmup=False)
        _tls.session = s
    return s


def _html_cache_path(symbol: str) -> str:
    return os.path.join(config.SCREENER_CACHE_DIR, f"{symbol}.html")


def _has_tables(html: str) -> bool:
    # Screener renders section IDs even for consolidated pages with no data.
    # Verify that the financial table theads actually contain date columns.
    soup = BeautifulSoup(html, "lxml")
    for sid in ("quarters", "profit-loss"):
        section = soup.find(id=sid)
        if section is None:
            continue
        table = section.find("table")
        if table is None:
            continue
        thead = table.find("thead")
        if thead and len(thead.find_all("th")) >= 2:
            return True
    return False


def fetch_company_page(symbol: str, force: bool = False) -> Optional[str]:
    cache_path = _html_cache_path(symbol)
    if not force and os.path.exists(cache_path):
        age_days = (date.today() - date.fromtimestamp(os.path.getmtime(cache_path))).days
        if age_days <= config.SCREENER_CACHE_DAYS:
            with open(cache_path, encoding="utf-8") as f:
                cached = f.read()
            if _has_tables(cached):
                logger.debug(f"{symbol}: using cached HTML ({age_days}d old)")
                return cached
            logger.debug(f"{symbol}: cached HTML has no tables, re-fetching")

    def _fetch_url(url: str) -> str:
        @retry(stop=stop_after_attempt(config.MAX_RETRIES), wait=wait_exponential(multiplier=2, min=30, max=90), reraise=True)
        def _get():
            session = _thread_session()
            r = session.get(url, timeout=30)
            if r.status_code in (429, 403):
                polite_sleep(60, 90)
                raise IOError(f"HTTP {r.status_code} for {symbol}")
            r.raise_for_status()
            return r.text
        return _get()

    urls = [
        f"{SCREENER_BASE}/company/{symbol}/consolidated/",
        f"{SCREENER_BASE}/company/{symbol}/",
    ]
    html = None
    for url in urls:
        try:
            candidate = _fetch_url(url)
            if _has_tables(candidate):
                html = candidate
                logger.debug(f"{symbol}: financial tables found at {url}")
                break
            logger.debug(f"{symbol}: no tables at {url}, trying standalone")
            polite_sleep(config.MIN_SLEEP_SCREENER, config.MAX_SLEEP_SCREENER)
        except Exception as e:
            logger.warning(f"{symbol}: could not fetch {url} - {e}")

    if html is None:
        logger.warning(f"{symbol}: could not fetch screener page")
        return None

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(html)
    polite_sleep(config.MIN_SLEEP_SCREENER, config.MAX_SLEEP_SCREENER)
    return html


# ---------------------------------------------------------------------------
# HTML table parser
# ---------------------------------------------------------------------------

def _snake(s: str) -> str:
    s = str(s).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def parse_section_table(soup: BeautifulSoup, section_id: str, date_parser) -> Optional[pd.DataFrame]:
    section = soup.find(id=section_id)
    if section is None:
        return None
    table = section.find("table", class_="data-table") or section.find("table")
    if table is None:
        return None
    thead = table.find("thead")
    if thead is None:
        return None
    header_cells = thead.find_all("th")
    if len(header_cells) < 2:
        return None

    date_headers = [date_parser(th.get_text(strip=True)) for th in header_cells[1:]]

    tbody = table.find("tbody")
    if tbody is None:
        return None

    records: dict = {}
    for tr in tbody.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        line_item = _snake(cells[0].get_text(strip=True))
        if not line_item:
            continue
        values = {}
        for i, td in enumerate(cells[1:]):
            if i >= len(date_headers) or date_headers[i] is None:
                continue
            raw = td.get_text(strip=True).replace(",", "").replace("%", "")
            try:
                values[date_headers[i]] = float(raw)
            except (ValueError, TypeError):
                pass
        if values:
            records[line_item] = values

    if not records:
        return None

    # pd.DataFrame(records): outer keys (parameters) → columns, inner keys (dates) → index
    df = pd.DataFrame(records)
    df.index = pd.DatetimeIndex(df.index)
    df.sort_index(inplace=True)
    return df


def parse_html_financials(html: str, symbol: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    result = {}

    # --- Quarterly ---
    q_frames = []
    for sid in QUARTERLY_SECTIONS:
        df = parse_section_table(soup, sid, parse_quarter_date)
        if df is not None and not df.empty:
            q_frames.append(df)

    if q_frames:
        wide = pd.concat(q_frames, axis=1, sort=True)
        wide = wide.loc[:, ~wide.columns.duplicated()]
        wide = normalize_wide(wide, is_annual=False)
        long = wide_to_long(wide, symbol)
        if long is not None:
            result["quarterly"] = long

    # --- Annual ---
    a_frames = []
    for sid in ANNUAL_SECTIONS:
        df = parse_section_table(soup, sid, parse_annual_date)
        if df is not None and not df.empty:
            a_frames.append(df)

    if a_frames:
        wide = pd.concat(a_frames, axis=1, sort=True)
        wide = wide.loc[:, ~wide.columns.duplicated()]
        wide = normalize_wide(wide, is_annual=True)
        long = wide_to_long(wide, symbol)
        if long is not None:
            result["annual"] = long

    return result


# ---------------------------------------------------------------------------
# Panel save
# ---------------------------------------------------------------------------

def _save_panel(new_df: Optional[pd.DataFrame], panel_path: str, label: str):
    if new_df is None or new_df.empty:
        logger.info(f"{label}: no new data to write.")
        return
    floor = financials_history_floor()
    new_df = new_df[pd.to_datetime(new_df["date"]) >= floor].copy()
    existing = read_long_panel(panel_path)
    combined = merge_long_panel(existing, new_df)
    write_long_panel(combined, panel_path)
    logger.info(f"{label} saved: {len(combined)} rows -> {panel_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _worker(symbol: str, force: bool, floor_ts: pd.Timestamp) -> dict:
    """
    Fetch + parse one symbol. Returns a dict with optional 'quarterly', 'annual', 'failure' keys.
    Pure function (no shared state) — safe to run in a thread pool.

    'failure' (if present) is a tuple (reason, error_msg).
    """
    out: dict = {"symbol": symbol}

    html = fetch_company_page(symbol, force=force)
    if not html:
        out["failure"] = ("PageFetchFailed", "Could not download screener page")
        return out

    parsed = parse_html_financials(html, symbol)

    if not parsed:
        out["failure"] = ("ParseFailed", "No financial tables parsed from HTML")
        return out

    if "quarterly" in parsed:
        q_df = parsed["quarterly"]
        if not q_df[pd.to_datetime(q_df["date"]) >= floor_ts].empty:
            out["quarterly"] = q_df
        else:
            out["failure"] = ("NoRecentData", f"All quarterly dates before financials floor {floor_ts.date()}")
    else:
        out["failure"] = ("NoQuarterlyData", "No quarterly P&L section on screener page")

    if "annual" in parsed:
        out["annual"] = parsed["annual"]

    return out


def main():
    parser = argparse.ArgumentParser(description="Fetch quarterly financials from screener.in")
    parser.add_argument("--symbols", type=str, default=None)
    parser.add_argument("--force", action="store_true", help="Ignore cache; re-download HTML")
    parser.add_argument("--workers", type=int, default=config.FINANCIALS_WORKERS,
                        help=f"Parallel worker threads (default {config.FINANCIALS_WORKERS} from config.FINANCIALS_WORKERS)")
    args = parser.parse_args()

    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.SCREENER_CACHE_DIR, exist_ok=True)

    metadata_path = path("metadata.csv")
    if not os.path.exists(metadata_path):
        logger.error("metadata.csv not found. Run universe.py first.")
        raise SystemExit(1)

    metadata = pd.read_csv(metadata_path)
    symbols = metadata["symbol"].dropna().unique().tolist()

    subset = parse_symbol_list(args.symbols)
    if subset:
        symbols = [s for s in symbols if s in subset]
        logger.info(f"Symbol subset: {symbols}")

    q_panel_path = path("financials_panel.csv")
    a_panel_path = path("financials_annual_panel.csv")
    floor_ts = financials_history_floor()

    all_quarterly, all_annual = [], []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_worker, sym, args.force, floor_ts): sym
            for sym in symbols
        }
        for fut in tqdm(as_completed(futures), total=len(symbols), desc="Fetching financials"):
            sym = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                logger.error(f"{sym}: unhandled error: {e}")
                log_gap("fetch_financials", sym, "WorkerException", str(e))
                continue

            if "quarterly" in result:
                all_quarterly.append(result["quarterly"])
            if "annual" in result:
                all_annual.append(result["annual"])
            if "failure" in result:
                reason, msg = result["failure"]
                logger.warning(f"{sym}: {reason} — {msg}")
                log_gap("fetch_financials", sym, reason, msg)

    quarterly_df = pd.concat(all_quarterly, ignore_index=True) if all_quarterly else None
    annual_df    = pd.concat(all_annual,    ignore_index=True) if all_annual    else None

    _save_panel(quarterly_df, q_panel_path, "financials_panel (quarterly)")
    _save_panel(annual_df,    a_panel_path, "financials_annual_panel")

    logger.info("fetch_financials complete.")


if __name__ == "__main__":
    main()
