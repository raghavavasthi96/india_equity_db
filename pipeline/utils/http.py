"""
utils/http.py — Canonical HTTP session factory and polite-sleep helper.

All NSE/screener fetchers should use `make_session()` to get a curl_cffi session
with chrome120 impersonation. `warmup=True` (default) hits nseindia.com once so
subsequent NSE API calls don't fail on cold cookies.
"""

import random
import time

from curl_cffi import requests as cffi_requests

_NSE_WARMUP_URL = "https://www.nseindia.com/"


def make_session(warmup: bool = True) -> cffi_requests.Session:
    s = cffi_requests.Session(impersonate="chrome120")
    if warmup:
        try:
            s.get(_NSE_WARMUP_URL, timeout=15)
        except Exception:
            pass
    return s


def polite_sleep(min_s: float, max_s: float) -> float:
    duration = random.uniform(min_s, max_s)
    time.sleep(duration)
    return duration
