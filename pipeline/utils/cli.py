"""
utils/cli.py — Shared CLI helpers.
"""

from typing import List, Optional


def parse_symbol_list(s: Optional[str]) -> Optional[List[str]]:
    """Parse comma-separated --symbols arg into a list. Returns None if `s` is None."""
    if s is None:
        return None
    return [sym.strip().upper() for sym in s.split(",") if sym.strip()]
