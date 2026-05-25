import os
from typing import Optional

import pandas as pd

import config

LONG_COLS = ["date", "symbol", "parameter", "value"]


def read_csv_if_exists(path: str, **kwargs) -> Optional[pd.DataFrame]:
    if os.path.exists(path):
        return pd.read_csv(path, **kwargs)
    return None


def read_long_panel(path: str) -> Optional[pd.DataFrame]:
    """Read a long-format panel CSV: date, symbol, parameter, value."""
    return read_csv_if_exists(path, parse_dates=["date"])


def write_long_panel(df: pd.DataFrame, path: str):
    """Write a long-format panel CSV, sorted by date, symbol, parameter."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df = df[LONG_COLS].sort_values(["date", "symbol", "parameter"]).reset_index(drop=True)
    df.to_csv(path, index=False)


def merge_long_panel(existing: Optional[pd.DataFrame], new_df: pd.DataFrame) -> pd.DataFrame:
    """Merge two long-format panels, deduplicating on (date, symbol, parameter), keeping newest."""
    if existing is None or existing.empty:
        return new_df
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined.drop_duplicates(subset=["date", "symbol", "parameter"], keep="last", inplace=True)
    combined.sort_values(["date", "symbol", "parameter"], inplace=True)
    combined.reset_index(drop=True, inplace=True)
    return combined


def wide_to_long(wide: pd.DataFrame, symbol: str) -> Optional[pd.DataFrame]:
    """
    Convert a (date-indexed, field-columns) DataFrame to long format for one symbol.
    Returns None if the result is empty after dropping NaNs.
    """
    df = wide.copy()
    df.index.name = "date"
    df = df.reset_index().melt(id_vars="date", var_name="parameter", value_name="value")
    df["symbol"] = symbol
    df = df[LONG_COLS].dropna(subset=["value"])
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    return df


def append_to_csv(row: dict, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    pd.DataFrame([row]).to_csv(path, mode="a", header=not exists, index=False)


def path(filename: str) -> str:
    return os.path.join(config.DATA_DIR, filename)
