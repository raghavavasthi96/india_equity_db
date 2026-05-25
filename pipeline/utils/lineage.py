"""
utils/lineage.py — Query corporate action lineage for a symbol.

Usage:
    python -m utils.lineage --symbol TATAMOTORS
    python -m utils.lineage --symbol HDFCBANK
    python -m utils.lineage --symbol RELIANCE
"""

import argparse
import os

import pandas as pd

from .io import path


def _load_tables():
    ca_path = path("corporate_actions.csv")
    sh_path = path("symbol_history.csv")

    ca = pd.read_csv(ca_path, parse_dates=["event_date"]) if os.path.exists(ca_path) else pd.DataFrame()
    sh = pd.read_csv(sh_path) if os.path.exists(sh_path) else pd.DataFrame()

    if not ca.empty:
        ca["_predecessors"] = ca["predecessor_symbols"].apply(
            lambda v: [s.strip() for s in str(v).split("|")] if pd.notna(v) else []
        )
        ca["_successors"] = ca["successor_symbols"].apply(
            lambda v: [s.strip() for s in str(v).split("|")] if pd.notna(v) else []
        )
    return ca, sh


def _symbol_info(symbol: str, sh: pd.DataFrame) -> str:
    if sh.empty:
        return symbol
    row = sh[sh["symbol"] == symbol]
    if row.empty:
        return f"{symbol} (unknown)"
    r = row.iloc[0]
    status = r.get("status", "unknown")
    isin = r.get("isin", "")
    isin_str = f", {isin}" if pd.notna(isin) and str(isin).strip() else ""
    return f"{symbol} ({status}{isin_str})"


def show_lineage(symbol: str) -> None:
    ca, sh = _load_tables()

    if ca.empty:
        print(f"corporate_actions.csv not found at {path('corporate_actions.csv')}")
        raise SystemExit(1)

    # Look up symbol in symbol_history.
    known_symbols = set(sh["symbol"].tolist()) if not sh.empty else set()
    if symbol not in known_symbols and not ca.empty:
        all_syms = set()
        for preds in ca["_predecessors"]:
            all_syms.update(preds)
        for succs in ca["_successors"]:
            all_syms.update(succs)
        if symbol not in all_syms:
            print(f"Symbol '{symbol}' not found in symbol_history.csv or corporate_actions.csv.")
            raise SystemExit(1)

    print(_symbol_info(symbol, sh))

    outgoing = ca[ca["_predecessors"].apply(lambda s: symbol in s)].sort_values("event_date")
    incoming = ca[ca["_successors"].apply(lambda s: symbol in s)].sort_values("event_date")

    if not outgoing.empty:
        print("Outgoing events:")
        for _, row in outgoing.iterrows():
            succs = "|".join(row["_successors"])
            ratio = f"  ratio: {row['ratio']}" if pd.notna(row.get("ratio")) and str(row.get("ratio")).strip() else ""
            print(f"  ├── {row['event_date'].date()} {row['event_type']} → {succs}{ratio}")
            print(f"  │     source: {row['source_url']}")

    if not incoming.empty:
        print("Incoming events:")
        for _, row in incoming.iterrows():
            preds = "|".join(row["_predecessors"])
            ratio = f"  ratio: {row['ratio']}" if pd.notna(row.get("ratio")) and str(row.get("ratio")).strip() else ""
            print(f"  └── {row['event_date'].date()} {row['event_type']} ← {preds}{ratio}")
            print(f"        source: {row['source_url']}")

    if outgoing.empty and incoming.empty:
        print("No corporate actions recorded.")


def main():
    parser = argparse.ArgumentParser(description="Show corporate action lineage for a symbol.")
    parser.add_argument("--symbol", required=True, help="NSE symbol (e.g. TATAMOTORS)")
    args = parser.parse_args()
    show_lineage(args.symbol.strip().upper())


if __name__ == "__main__":
    main()
