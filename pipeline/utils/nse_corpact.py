"""
utils/nse_corpact.py — Fetch + parse NSE corporate actions for one symbol.

Source: https://www.nseindia.com/api/corporates-corporateActions?symbol=X
Covers active and most delisted symbols. Some post-demerger symbols (e.g. TATAMOTORS)
return empty — recorded as data gaps.

Output: list[dict] with keys {symbol, ex_date, action_type, ratio_num, ratio_denom, amount, raw_subject}
action_type ∈ {split, bonus, dividend}. Other subjects (AGM, mergers, rights) are skipped.
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
from curl_cffi import requests as cffi_requests

from pipeline.configs import config
from .logger import get_logger
from .http import make_session, polite_sleep
from .dates import parse_filing_date

logger = get_logger("nse_corpact")

_API_URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities&symbol={symbol}"

_DIVIDEND_RE = re.compile(r"dividend", re.IGNORECASE)
_DIV_AMOUNT_RE = re.compile(r"rs\.?\s*\.?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_DIV_AMOUNT_RE_ALT = re.compile(r"re\.?\s*\.?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_BONUS_RE = re.compile(r"bonus", re.IGNORECASE)
_BONUS_RATIO_RE = re.compile(r"bonus[^\d]*(\d+)\s*[:\-/]\s*(\d+)", re.IGNORECASE)
_SPLIT_RE = re.compile(r"split|sub[\s-]?division|face\s*value", re.IGNORECASE)
# "Rs X (anything) to Rs Y" — covers "from Rs 10/- to Rs 2/-", "Rs 10/- Per Share To Rs 2/-",
# "Fv Split Rs.10/- To Rs.2/", etc. Non-greedy gap; capped at 40 chars to avoid runaway matches.
_SPLIT_FROM_TO_RE = re.compile(
    r"(?:rs|re)\.?\s*(\d+(?:\.\d+)?)[\s\S]{0,40}?to\s+(?:rs|re)\.?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
# Ratio formats: "Stock Split 1:5", "Sub-division 10:2", "Split 1:10".
_SPLIT_RATIO_RE = re.compile(r"(?:split|sub[\s-]?division)[^\d]{0,30}?(\d+)\s*[:\-/]\s*(\d+)", re.IGNORECASE)
# Face-value-only "Rs.10/- of Rs.2/-" or "Re.1/- of Rs.10/-"
_SPLIT_FV_OF_RE = re.compile(r"(?:rs|re)\.?\s*(\d+(?:\.\d+)?)\s*/?\-?\s*of\s+(?:rs|re)\.?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)

_SKIP_KEYWORDS = ("annual general meeting", "egm", "extraordinary", "merger", "amalgamation", "rights", "scheme of arrangement")


def _cache_path(symbol: str) -> Path:
    return Path(config.NSE_CORPACT_CACHE_DIR) / f"{symbol}.json"


def _fetch_raw(symbol: str, session: cffi_requests.Session) -> list:
    polite_sleep(config.MIN_SLEEP_NSE, config.MAX_SLEEP_NSE)
    url = _API_URL.format(symbol=symbol)
    r = session.get(url, timeout=30, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def get_corp_actions(symbol: str, session: Optional[cffi_requests.Session] = None, force: bool = False) -> list:
    """
    Fetch raw NSE corp action records for a symbol. Cached per-symbol for NSE_CORPACT_CACHE_DAYS days.
    Returns list of raw dicts from the NSE API.
    """
    cache_path = _cache_path(symbol)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not force and cache_path.exists():
        age_days = (date.today() - date.fromtimestamp(cache_path.stat().st_mtime)).days
        if age_days <= config.NSE_CORPACT_CACHE_DAYS:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)

    if session is None:
        session = make_session()

    try:
        raw = _fetch_raw(symbol, session)
    except Exception as e:
        logger.warning(f"{symbol}: corp action fetch failed - {e}")
        return []

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    return raw


def _parse_subject(subject: str) -> Optional[dict]:
    """
    Parse a raw NSE 'subject' string into (action_type, ratio_num, ratio_denom, amount).
    Returns None for subjects to skip (AGM, mergers, rights, unrecognized).
    """
    s = subject.strip().lower()
    if any(k in s for k in _SKIP_KEYWORDS) and not _DIVIDEND_RE.search(s):
        return None

    # Stock split / sub-division. Multiple subject formats; try each in order.
    # Returned ratio_num:ratio_denom is "old FV : new FV" so price_factor = denom/num < 1.
    if _SPLIT_RE.search(s):
        # 1. "Stock Split From Rs.10/- to Rs.2/-" → 5-for-1
        m = _SPLIT_FROM_TO_RE.search(subject)
        if m:
            from_fv, to_fv = float(m.group(1)), float(m.group(2))
            if to_fv > 0 and from_fv > to_fv:
                return {"action_type": "split", "ratio_num": from_fv, "ratio_denom": to_fv, "amount": None}
        # 2. "FV Rs.10/- of Rs.2/-" or "Rs.10 of Re.1"
        m = _SPLIT_FV_OF_RE.search(subject)
        if m:
            from_fv, to_fv = float(m.group(1)), float(m.group(2))
            if to_fv > 0 and from_fv > to_fv:
                return {"action_type": "split", "ratio_num": from_fv, "ratio_denom": to_fv, "amount": None}
        # 3. Pure ratio: "Stock Split 1:5" / "Sub-division 10:2".
        #    Disambiguate: split ratios are written either "old:new" (10:2)
        #    or "new:old" (1:5). Both real; the larger number is always old-FV.
        m = _SPLIT_RATIO_RE.search(subject)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 0 and b > 0 and a != b:
                old_fv, new_fv = (a, b) if a > b else (b, a)
                return {"action_type": "split", "ratio_num": old_fv, "ratio_denom": new_fv, "amount": None}
        return None

    # Bonus: "Bonus 1:1" or "Bonus issue 6:1" → ratio_num bonus per ratio_denom held
    if _BONUS_RE.search(s):
        m = _BONUS_RATIO_RE.search(subject)
        if m:
            num, denom = int(m.group(1)), int(m.group(2))
            if num > 0 and denom > 0:
                return {"action_type": "bonus", "ratio_num": num, "ratio_denom": denom, "amount": None}
        return None

    # Dividend: extract Rs amount. Skip if no parseable number.
    if _DIVIDEND_RE.search(s):
        m = _DIV_AMOUNT_RE.search(subject) or _DIV_AMOUNT_RE_ALT.search(subject)
        if m:
            amount = float(m.group(1))
            if amount > 0:
                return {"action_type": "dividend", "ratio_num": None, "ratio_denom": None, "amount": amount}
        return None

    return None


def get_adjustments(symbol: str, session: Optional[cffi_requests.Session] = None, force: bool = False) -> pd.DataFrame:
    """
    Return parsed corp action adjustments for a symbol as DataFrame.
    Columns: symbol, ex_date, action_type, ratio_num, ratio_denom, amount, source, raw_subject
    """
    raw = get_corp_actions(symbol, session=session, force=force)
    rows = []
    for r in raw:
        ex_date = parse_filing_date(r.get("exDate", ""))
        if ex_date is None:
            continue
        subject = r.get("subject", "")
        parsed = _parse_subject(subject)
        if parsed is None:
            continue
        rows.append({
            "symbol": symbol,
            "ex_date": ex_date,
            "action_type": parsed["action_type"],
            "ratio_num": parsed["ratio_num"],
            "ratio_denom": parsed["ratio_denom"],
            "amount": parsed["amount"],
            "source": "nse_corpact",
            "raw_subject": subject,
        })
    df = pd.DataFrame(rows, columns=["symbol", "ex_date", "action_type", "ratio_num", "ratio_denom", "amount", "source", "raw_subject"])
    if not df.empty:
        df = df.sort_values("ex_date").reset_index(drop=True)
    return df
