"""Nifty 500 Total Returns Index fetcher + cached loader.

Source: niftyindices.com `getTotalReturnIndexString` endpoint. Requires curl_cffi
to bypass NSE's bot guard. Caches the full TRI history as a CSV under
`data/nifty500_tri.csv` so backtests can run offline after one fetch.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from curl_cffi import requests as creq

from backtest.configs.config import BACKTEST_DATA_DIR

BENCHMARK_NAME = "NIFTY 500"
DEFAULT_PATH = os.path.join(BACKTEST_DATA_DIR, "nifty500_tri.csv")
ENDPOINT = "https://www.niftyindices.com/Backpage.aspx/getTotalReturnIndexString"
PAGE = "https://www.niftyindices.com/reports/historical-data"

# Niftyindices API only returns ~365 days per request — chunk the call.
CHUNK_DAYS = 360


def _fetch_chunk(session, name: str, start: date, end: date) -> pd.DataFrame:
    payload = {
        "cinfo": json.dumps({
            "name": name,
            "startDate": start.strftime("%d-%b-%Y"),
            "endDate": end.strftime("%d-%b-%Y"),
            "indexName": name,
        })
    }
    r = session.post(
        ENDPOINT, json=payload,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        timeout=30,
    )
    r.raise_for_status()
    outer = r.json()
    rows = json.loads(outer["d"])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.rename(columns={"Date": "date", "TotalReturnsIndex": "close"})
    df = df[["date", "close"]]
    df["date"] = pd.to_datetime(df["date"], format="%d %b %Y")
    df["close"] = df["close"].astype(float)
    return df


def fetch_nifty500_tri(
    start: str = "2010-01-01",
    end: Optional[str] = None,
    out_path: str = DEFAULT_PATH,
) -> str:
    """Fetch the full Nifty 500 TRI history and save as CSV. Returns the saved path."""
    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = (
        datetime.strptime(end, "%Y-%m-%d").date()
        if end else date.today()
    )

    session = creq.Session(impersonate="chrome")
    session.get(PAGE, timeout=15)  # cookie warmup

    parts = []
    cur = start_d
    while cur < end_d:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS), end_d)
        df = _fetch_chunk(session, BENCHMARK_NAME, cur, chunk_end)
        if not df.empty:
            parts.append(df)
            print(f"  fetched {cur}..{chunk_end} ({len(df)} rows)")
        cur = chunk_end + timedelta(days=1)

    if not parts:
        raise RuntimeError("No data returned from niftyindices.com")

    out = pd.concat(parts).drop_duplicates(subset=["date"]).sort_values("date")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    out.to_csv(out_path, index=False)
    print(f"[done] {len(out):,} rows -> {out_path}")
    return out_path


def load_nifty500_tri(path: str = DEFAULT_PATH) -> pd.Series:
    """Load TRI close as a daily-indexed Series. Raises with instructions if missing."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Nifty 500 TRI file not found at: {path}\n"
            f"Fetch it with: python -m backtest.utils.benchmark"
        )
    df = pd.read_csv(path, parse_dates=["date"])
    s = df.set_index("date")["close"].sort_index()
    s.name = "nifty500_tri"
    return s


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=DEFAULT_PATH)
    args = ap.parse_args()
    fetch_nifty500_tri(start=args.start, end=args.end, out_path=args.out)
