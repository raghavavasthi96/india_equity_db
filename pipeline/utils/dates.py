"""
utils/dates.py — Canonical date parsing and quarter-end helpers.
"""

import calendar
import re
from datetime import date, datetime
from typing import List, Optional

import pandas as pd

from pipeline.configs import config


_FILING_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d")


def month_end(year: int, month: int) -> pd.Timestamp:
    return pd.Timestamp(year, month, calendar.monthrange(year, month)[1])


def parse_filing_date(s: str) -> Optional[date]:
    """Parse NSE/screener filing-date strings. Tries multiple known formats."""
    if not s or s == "-":
        return None
    s = s.strip()
    for fmt in _FILING_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_quarter_date(s: str) -> Optional[pd.Timestamp]:
    """'Mar 2024' -> 2024-03-31, 'Jun 2024' -> 2024-06-30, etc."""
    s = re.sub(r"\s*TTM\s*$", "", s.strip(), flags=re.I)
    try:
        dt = pd.to_datetime(s, format="%b %Y")
        return month_end(dt.year, dt.month)
    except Exception:
        return None


def parse_annual_date(s: str) -> Optional[pd.Timestamp]:
    """Force all annual dates to 31-Mar-YYYY (Indian fiscal year end convention)."""
    s = re.sub(r"\s*TTM\s*$", "", s.strip(), flags=re.I)
    try:
        dt = pd.to_datetime(s, format="%b %Y")
        return month_end(dt.year, 3)
    except Exception:
        return None


def enforce_project_floor(start: date) -> date:
    """Raise SystemExit if `start` precedes PROJECT_START_DATE."""
    floor = date.fromisoformat(config.PROJECT_START_DATE)
    if start < floor:
        raise SystemExit(
            f"start_date {start} is before PROJECT_START_DATE {floor}. "
            "Overrides may move the start later, never earlier."
        )
    return start


def price_history_floor() -> date:
    """Absolute earliest date prices may be fetched from: PROJECT_START_DATE
    minus the configured signal-lookback buffer. Prices and financials are the
    two artifacts allowed below PROJECT_START_DATE — universe and XBRL stay
    floored. See `financials_history_floor()` for the financials analogue."""
    from datetime import timedelta
    return (
        date.fromisoformat(config.PROJECT_START_DATE)
        - timedelta(days=config.PRICE_HISTORY_LOOKBACK_DAYS)
    )


def enforce_price_history_floor(start: date) -> date:
    """Raise SystemExit if `start` precedes the lookback-extended price floor."""
    floor = price_history_floor()
    if start < floor:
        raise SystemExit(
            f"start_date {start} is before the price-history floor {floor} "
            f"(PROJECT_START_DATE − {config.PRICE_HISTORY_LOOKBACK_DAYS}d). "
            "Lower PRICE_HISTORY_LOOKBACK_DAYS to extend further back."
        )
    return start


def financials_history_floor() -> pd.Timestamp:
    """Earliest quarter-end date retained in financials panels: PROJECT_START_DATE
    minus FINANCIALS_HISTORY_LOOKBACK_QUARTERS quarters. Gives YoY / multi-quarter
    signals a prior-year reading at the first backtest rebal date. Financials and
    prices are the two artifacts allowed below PROJECT_START_DATE — universe / XBRL
    stay floored at PROJECT_START_DATE."""
    quarters = config.FINANCIALS_HISTORY_LOOKBACK_QUARTERS
    return pd.Timestamp(config.PROJECT_START_DATE) - pd.DateOffset(months=quarters * 3)


def quarter_end_dates(start_date: date, end_date: date) -> List[date]:
    dates = []
    for year in range(start_date.year, end_date.year + 1):
        for month, day in [(3, 31), (6, 30), (9, 30), (12, 31)]:
            d = date(year, month, day)
            if start_date <= d <= end_date:
                dates.append(d)
    return dates
