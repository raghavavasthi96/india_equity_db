"""
fetch_shp_xbrl.py — Fetch NSE Shareholding Pattern XBRL filings for universe symbols.

Writes data/shares_outstanding.csv with columns:
  symbol, date, total_shares, promoter_shares, free_float_shares, source

Run:
    python fetch_shp_xbrl.py [--symbols A,B,C] [--force] [--start-date YYYY-MM-DD]
"""

import argparse
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
from curl_cffi import requests as cffi_requests
from tqdm import tqdm

import config
from utils.logger import get_logger
from utils.http import make_session, polite_sleep
from utils.cli import parse_symbol_list
from utils.dates import parse_filing_date, enforce_project_floor
from utils.gaps import log_gap
from utils.io import read_csv_if_exists, path

logger = get_logger("fetch_shp_xbrl")

SHP_INDEX_URL = (
    "https://www.nseindia.com/api/corporate-share-holdings-master"
    "?index=equities&symbol={symbol}"
)
_WARMUP_URLS = [
    "https://www.nseindia.com/",
    "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern",
]
_API_HEADERS = {
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern",
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}
_XBRL_HEADERS = {
    "Referer": "https://www.nseindia.com/",
}

_CTX_PATTERNS = {
    "total":        r'ShareholdingPattern(?:I|_ContextI)',
    "promoter":     r'ShareholdingOfPromoterAndPromoterGroup(?:I|_ContextI)',
    "public":       r'PublicShareholding(?:I|_ContextI)',
    "non_pub_nonp": r'SharesHeldByNonPromoterNonPublicShareholders(?:I|_ContextI)',
}


def _warmup(session: cffi_requests.Session) -> None:
    for url in _WARMUP_URLS:
        try:
            session.get(url, timeout=20)
            polite_sleep(config.MIN_SLEEP_NSE, config.MAX_SLEEP_NSE)
        except Exception as e:
            logger.warning(f"Warmup GET {url} failed: {e}")


def _xbrl_cache_path(symbol: str, filing_date: date, record_id) -> Path:
    date_str = filing_date.strftime("%Y-%m-%d")
    return Path(config.XBRL_CACHE_DIR) / symbol / f"{date_str}_{record_id}.xml"


def _index_cache_path(symbol: str) -> Path:
    return Path(config.XBRL_CACHE_DIR) / symbol / "_index.json"


def _get_filings_index(symbol: str, session: cffi_requests.Session, force: bool) -> list:
    cache_path = _index_cache_path(symbol)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not force and cache_path.exists():
        age_days = (date.today() - date.fromtimestamp(cache_path.stat().st_mtime)).days
        if age_days <= config.XBRL_INDEX_CACHE_DAYS:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)

    url = SHP_INDEX_URL.format(symbol=symbol)
    polite_sleep(config.MIN_SLEEP_NSE, config.MAX_SLEEP_NSE)
    try:
        r = session.get(url, headers=_API_HEADERS, timeout=30)
        if r.status_code in (403, 429):
            logger.warning(f"{symbol}: HTTP {r.status_code} on index fetch")
            return []
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"{symbol}: index fetch failed: {e}")
        return []

    filings = data if isinstance(data, list) else data.get("data", [])
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(filings, f)
    return filings


def _extract_share_count(txt: str, ctx_pattern: str) -> Optional[int]:
    m = re.search(
        rf'<[a-zA-Z0-9_-]+:NumberOfShares\s+[^>]*contextRef="{ctx_pattern}"[^>]*>([^<]+)<',
        txt,
    )
    return int(m.group(1)) if m else None


def _parse_xbrl_shares(xml_bytes: bytes, symbol: str, filing_date: date) -> Optional[dict]:
    txt = xml_bytes.decode("utf-8", errors="ignore")
    if len(txt) < 5000:
        logger.debug(f"{symbol} {filing_date}: XML too small ({len(txt)} bytes), skipping")
        return None

    total = _extract_share_count(txt, _CTX_PATTERNS["total"])
    promoter = _extract_share_count(txt, _CTX_PATTERNS["promoter"])
    public = _extract_share_count(txt, _CTX_PATTERNS["public"])
    other = _extract_share_count(txt, _CTX_PATTERNS["non_pub_nonp"])

    if total is None:
        logger.warning(f"{symbol} {filing_date}: missing total, skipping")
        return None

    if promoter is None:
        # NSE omits the promoter context entirely when promoter shareholding is 0
        # (observed in HDFCBANK post-2025 filings). Distinguish from a genuine parse
        # failure by checking whether the context name appears at all in the XML.
        if "ShareholdingOfPromoterAndPromoterGroup" not in txt:
            promoter = 0
        else:
            logger.warning(f"{symbol} {filing_date}: promoter context present but unparseable, skipping")
            return None

    free_float_sub = total - promoter
    if public is not None and other is not None:
        free_float_add = public + other
        if abs(free_float_add - free_float_sub) > 1:
            logger.warning(
                f"{symbol} {filing_date}: free-float mismatch "
                f"(sub={free_float_sub}, add={free_float_add}); using subtraction"
            )

    return {
        "symbol": symbol,
        "date": filing_date,
        "total_shares": total,
        "promoter_shares": promoter,
        "public_shares": public,
        "non_pub_nonp_shares": other,
        "free_float_shares": free_float_sub,
        "source": "nse_shp_xbrl",
    }


def _fetch_symbol(symbol: str, session: cffi_requests.Session, start_date: date, force: bool) -> pd.DataFrame:
    filings = _get_filings_index(symbol, session, force)
    if not filings:
        logger.warning(f"{symbol}: no filings in index — NSE returned empty; will record in data_gaps.csv")
        return pd.DataFrame()

    rows = []
    for filing in filings:
        xbrl_url = filing.get("xbrl") or ""
        url_tail = xbrl_url.rsplit("/", 1)[-1]
        if url_tail in ("", "-", "null"):
            logger.debug(f"{symbol}: skipping placeholder XBRL {url_tail!r}")
            continue

        date_str = filing.get("date", "")
        filing_date = parse_filing_date(date_str)
        if filing_date is None:
            logger.debug(f"{symbol}: could not parse date {date_str!r}")
            continue
        if filing_date < start_date:
            continue

        record_id = filing.get("recordId", filing.get("record_id", "0"))
        cache_path = _xbrl_cache_path(symbol, filing_date, record_id)
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        if force or not cache_path.exists():
            polite_sleep(config.MIN_SLEEP_NSE, config.MAX_SLEEP_NSE)
            try:
                r = session.get(xbrl_url, headers=_XBRL_HEADERS, timeout=60)
                if r.status_code in (403, 429):
                    logger.warning(f"{symbol} {filing_date}: HTTP {r.status_code}, skipping")
                    continue
                r.raise_for_status()
                cache_path.write_bytes(r.content)
            except Exception as e:
                logger.warning(f"{symbol} {filing_date}: XBRL download failed: {e}")
                continue

        try:
            xml_bytes = cache_path.read_bytes()
        except Exception as e:
            logger.warning(f"{symbol} {filing_date}: could not read cache: {e}")
            continue

        parsed = _parse_xbrl_shares(xml_bytes, symbol, filing_date)
        if parsed is not None:
            rid_str = str(record_id)
            parsed["_record_id"] = int(rid_str) if rid_str.isdigit() else 0
            rows.append(parsed)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Dedup on (symbol, date) keeping the highest recordId (latest/revised filing).
    df = (
        df.sort_values("_record_id")
        .drop_duplicates(subset=["symbol", "date"], keep="last")
        .drop(columns=["_record_id"])
    )
    return df


_tls = threading.local()


def _thread_session() -> cffi_requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = make_session(warmup=False)
        _warmup(s)
        _tls.session = s
    return s


def _worker(symbol: str, start_date: date, force: bool) -> pd.DataFrame:
    return _fetch_symbol(symbol, _thread_session(), start_date, force)


def main():
    parser = argparse.ArgumentParser(description="Fetch NSE SHP XBRL filings for shares outstanding.")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated symbol subset")
    parser.add_argument("--force", action="store_true", help="Re-download all XBRL files (ignore cache)")
    parser.add_argument("--start-date", type=str, default=None,
                        help=f"Skip filings before this date YYYY-MM-DD (>= {config.PROJECT_START_DATE})")
    parser.add_argument("--workers", type=int, default=config.XBRL_WORKERS,
                        help=f"Parallel worker threads (default {config.XBRL_WORKERS} from config.XBRL_WORKERS)")
    args = parser.parse_args()

    if args.start_date:
        start_date = enforce_project_floor(date.fromisoformat(args.start_date))
    else:
        start_date = date.fromisoformat(config.PROJECT_START_DATE)

    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.XBRL_CACHE_DIR, exist_ok=True)

    metadata_path = path("metadata.csv")
    if not os.path.exists(metadata_path):
        logger.error("metadata.csv not found. Run universe.py --bootstrap first.")
        raise SystemExit(1)

    metadata = pd.read_csv(metadata_path)
    symbols = metadata["symbol"].dropna().unique().tolist()

    subset = parse_symbol_list(args.symbols)
    if subset:
        symbols = [s for s in symbols if s in subset]
        logger.info(f"Symbol subset: {subset}")

    shares_path = path("shares_outstanding.csv")
    existing = read_csv_if_exists(shares_path, parse_dates=["date"])

    all_frames = []
    no_data_symbols = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_worker, sym, start_date, args.force): sym for sym in symbols}
        for fut in tqdm(as_completed(futures), total=len(symbols), desc="Fetching XBRL"):
            sym = futures[fut]
            try:
                df = fut.result()
                if not df.empty:
                    all_frames.append(df)
                else:
                    no_data_symbols.append(sym)
            except Exception as e:
                logger.error(f"{sym}: unhandled error: {e}")
                no_data_symbols.append(sym)

    if no_data_symbols:
        logger.warning(
            f"{len(no_data_symbols)} symbols returned no XBRL data: {no_data_symbols[:10]}"
            + (" ..." if len(no_data_symbols) > 10 else "")
        )
        for sym in no_data_symbols:
            log_gap("fetch_shp_xbrl", sym, "xbrl_unavailable_for_delisted")

    if not all_frames:
        logger.warning("No XBRL data fetched.")
        return

    new_data = pd.concat(all_frames, ignore_index=True)
    new_data["date"] = pd.to_datetime(new_data["date"])

    if existing is not None and not existing.empty:
        if "total_shares" not in existing.columns:
            backup = shares_path + ".bak"
            existing.to_csv(backup, index=False)
            logger.warning(
                f"Existing shares_outstanding.csv has old schema (no total_shares column); "
                f"backed up to {backup} and replacing entirely."
            )
            combined = new_data
        else:
            existing["date"] = pd.to_datetime(existing["date"])
            # Align columns: new schema may have public_shares/non_pub_nonp_shares that old rows lack.
            combined = pd.concat([existing, new_data], ignore_index=True)
            combined.drop_duplicates(subset=["symbol", "date"], keep="last", inplace=True)
    else:
        combined = new_data

    combined.sort_values(["symbol", "date"], inplace=True)
    combined.reset_index(drop=True, inplace=True)
    combined.to_csv(shares_path, index=False)
    logger.info(f"shares_outstanding saved: {len(combined)} rows -> {shares_path}")
    logger.info("fetch_shp_xbrl complete.")


if __name__ == "__main__":
    main()
