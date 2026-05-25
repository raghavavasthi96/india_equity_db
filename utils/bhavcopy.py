import io
import os
import zipfile
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from curl_cffi import requests as cffi_requests
from tqdm import tqdm

import config
from utils.logger import get_logger
from utils.http import make_session, polite_sleep

logger = get_logger("bhavcopy")

_ARCHIVE_URL = (
    "https://nsearchives.nseindia.com/content/historical/EQUITIES"
    "/{year}/{mon}/cm{dd}{mon}{year}bhav.csv.zip"
)
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_NSELIB_CUTOFF = date(2020, 1, 1)

_OUT_COLS = ["symbol", "series", "date", "open", "high", "low",
             "close", "prev_close", "volume", "turnover_lacs"]


def _normalize(df: pd.DataFrame, d: date, rename_map: dict) -> pd.DataFrame:
    df.columns = [c.strip() for c in df.columns]
    series_col = next((c for c in df.columns if c.upper() == "SERIES"), None)
    if series_col:
        df = df[df[series_col].str.strip() == "EQ"].copy()
    df = df.rename(columns=rename_map)
    df["date"] = pd.Timestamp(d)
    for c in _OUT_COLS:
        if c not in df.columns:
            df[c] = None
    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str).str.strip()
    return df[_OUT_COLS].reset_index(drop=True)


def _fetch_nselib(d: date) -> pd.DataFrame:
    from nselib.capital_market import bhav_copy_with_delivery
    df = bhav_copy_with_delivery(d.strftime("%d-%m-%Y"))
    if df is None or (hasattr(df, "empty") and df.empty):
        raise FileNotFoundError(f"nselib returned no data for {d}")
    rename = {
        "SYMBOL": "symbol", "SERIES": "series",
        "OPEN_PRICE": "open", "HIGH_PRICE": "high",
        "LOW_PRICE": "low", "CLOSE_PRICE": "close",
        "PREV_CLOSE": "prev_close",
        "TTL_TRD_QNTY": "volume", "TURNOVER_LACS": "turnover_lacs",
    }
    return _normalize(df, d, rename)


def _fetch_archive(d: date, session: cffi_requests.Session) -> pd.DataFrame:
    mon = _MONTHS[d.month - 1]
    dd = d.strftime("%d")
    year = d.strftime("%Y")
    url = _ARCHIVE_URL.format(year=year, mon=mon, dd=dd)
    headers = {
        "Referer": "https://www.nseindia.com/",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = session.get(url, headers=headers, timeout=30)
    if r.status_code == 404:
        raise FileNotFoundError(f"No bhavcopy archive for {d} (404)")
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        with zf.open(zf.namelist()[0]) as f:
            df = pd.read_csv(f)
    rename = {
        "SYMBOL": "symbol", "SERIES": "series",
        "OPEN": "open", "HIGH": "high", "LOW": "low", "CLOSE": "close",
        "PREVCLOSE": "prev_close",
        "TOTTRDQTY": "volume", "TOTTRDVAL": "turnover_lacs",
    }
    return _normalize(df, d, rename)


def fetch_bhavcopy(d: date, session=None) -> pd.DataFrame:
    """
    Return EQ-only bhavcopy for date d.
    Columns: symbol, series, date, open, high, low, close, prev_close, volume, turnover_lacs
    Routes to nselib for d >= 2020-01-01, archive URL otherwise.
    Raises FileNotFoundError for holidays/weekends with no data.
    """
    os.makedirs(config.BHAVCOPY_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(config.BHAVCOPY_CACHE_DIR, f"{d.strftime('%Y%m%d')}.parquet")
    if os.path.exists(cache_path):
        return pd.read_parquet(cache_path)

    polite_sleep(config.MIN_SLEEP_NSE, config.MAX_SLEEP_NSE)

    if d >= _NSELIB_CUTOFF:
        try:
            df = _fetch_nselib(d)
        except FileNotFoundError:
            raise
        except Exception as e:
            logger.warning(f"bhavcopy {d}: nselib failed ({e}); falling back to archive")
            if session is None:
                session = make_session()
            df = _fetch_archive(d, session)
    else:
        if session is None:
            session = make_session()
        df = _fetch_archive(d, session)

    df.to_parquet(cache_path, index=False)
    logger.debug(f"bhavcopy {d}: {len(df)} EQ rows cached")
    return df


def fetch_daily_range(symbol: str, start: date, end: date, session: Optional[cffi_requests.Session] = None,
                      show_progress: bool = False) -> pd.DataFrame:
    """
    Return daily OHLCV rows for `symbol` across [start, end].
    Iterates trading days, fetching missing bhavcopy parquets into the cache.
    Skips weekends and tolerates holidays. Returns DataFrame indexed by date
    with columns [open, high, low, close, volume]. Empty DF if symbol never appears.
    """
    if session is None and start < _NSELIB_CUTOFF:
        session = make_session()

    rows = []
    days = (end - start).days + 1
    iterator = range(days)
    if show_progress:
        iterator = tqdm(iterator, desc=f"bhavcopy {symbol}", leave=False)

    for offset in iterator:
        d = start + timedelta(days=offset)
        if d.weekday() >= 5:
            continue
        try:
            bdf = fetch_bhavcopy(d, session=session)
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.debug(f"{symbol}: bhavcopy {d} error - {e}")
            continue
        sub = bdf[bdf["symbol"] == symbol]
        if sub.empty:
            continue
        r = sub.iloc[0]
        rows.append({
            "date": pd.Timestamp(d),
            "open": r.get("open"),
            "high": r.get("high"),
            "low": r.get("low"),
            "close": r.get("close"),
            "volume": r.get("volume"),
        })

    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows).set_index("date").sort_index()
    return df
