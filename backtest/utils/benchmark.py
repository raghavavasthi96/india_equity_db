"""Total Returns Index fetcher + cached loader for benchmark indices.

Source: niftyindices.com `getTotalReturnIndexString` endpoint. Requires curl_cffi
to bypass NSE's bot guard. Caches each index's full TRI history as a CSV under
`data/` (one file per index, see `BENCHMARKS`) so backtests can run offline
after one fetch.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from curl_cffi import requests as creq

from backtest.configs.config import BACKTEST_DATA_DIR

# config key -> (niftyindices index name, cache filename). The API name must
# match exactly: an unknown name returns an empty list, not an error.
BENCHMARKS = {
    "nifty500_tri": ("NIFTY 500", "nifty500_tri.csv"),
    "nifty500_momentum50_tri": ("NIFTY500 MOMENTUM 50", "nifty500_momentum50_tri.csv"),
}
DEFAULT_BENCHMARK = "nifty500_tri"

BENCHMARK_NAME = BENCHMARKS[DEFAULT_BENCHMARK][0]
DEFAULT_PATH = os.path.join(BACKTEST_DATA_DIR, BENCHMARKS[DEFAULT_BENCHMARK][1])
ENDPOINT = "https://www.niftyindices.com/BackPage/getTotalReturnIndexString"
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
    rows = r.json()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.rename(columns={"Date": "date", "TotalReturnsIndex": "close"})
    df = df[["date", "close"]]
    df["date"] = pd.to_datetime(df["date"], format="%d %b %Y")
    df["close"] = df["close"].astype(float)
    return df


def benchmark_path(benchmark: str = DEFAULT_BENCHMARK) -> str:
    """Cache path for a registered benchmark. Raises on an unknown key."""
    if benchmark not in BENCHMARKS:
        raise KeyError(
            f"Unknown benchmark {benchmark!r}. Known: {sorted(BENCHMARKS)}"
        )
    return os.path.join(BACKTEST_DATA_DIR, BENCHMARKS[benchmark][1])


def fetch_tri(
    benchmark: str = DEFAULT_BENCHMARK,
    start: str = "2010-01-01",
    end: Optional[str] = None,
    out_path: Optional[str] = None,
) -> str:
    """Fetch a benchmark's full TRI history and save as CSV. Returns the saved path."""
    out_path = out_path or benchmark_path(benchmark)
    index_name = BENCHMARKS[benchmark][0]
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
        df = _fetch_chunk(session, index_name, cur, chunk_end)
        if not df.empty:
            parts.append(df)
            print(f"  fetched {cur}..{chunk_end} ({len(df)} rows)")
        cur = chunk_end + timedelta(days=1)

    if not parts:
        raise RuntimeError(f"No data returned from niftyindices.com for {index_name!r}")

    out = pd.concat(parts).drop_duplicates(subset=["date"]).sort_values("date")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    out.to_csv(out_path, index=False)
    print(f"[done] {len(out):,} rows -> {out_path}")
    return out_path


def load_tri(
    benchmark: str = DEFAULT_BENCHMARK,
    path: Optional[str] = None,
) -> pd.Series:
    """Load TRI close as a daily-indexed Series. Raises with instructions if missing.

    `path` overrides the registry location — use it for an arbitrary TRI CSV.
    """
    path = path or benchmark_path(benchmark)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"TRI file for {benchmark!r} not found at: {path}\n"
            f"Fetch it with: python -m backtest.utils.benchmark --index {benchmark}"
        )
    df = pd.read_csv(path, parse_dates=["date"])
    s = df.set_index("date")["close"].sort_index()
    s.name = benchmark
    return s


def fetch_nifty500_tri(
    start: str = "2010-01-01",
    end: Optional[str] = None,
    out_path: str = DEFAULT_PATH,
) -> str:
    """Back-compat wrapper for `fetch_tri("nifty500_tri", ...)`."""
    return fetch_tri(DEFAULT_BENCHMARK, start=start, end=end, out_path=out_path)


def load_nifty500_tri(path: str = DEFAULT_PATH) -> pd.Series:
    """Back-compat wrapper for `load_tri("nifty500_tri", ...)`."""
    return load_tri(DEFAULT_BENCHMARK, path=path)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default=DEFAULT_BENCHMARK, choices=sorted(BENCHMARKS))
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    fetch_tri(args.index, start=args.start, end=args.end, out_path=args.out)
