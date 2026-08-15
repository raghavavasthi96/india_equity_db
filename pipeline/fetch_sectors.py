"""
fetch_sectors.py — Resolve the NSE/BSE unified industry classification for every
universe symbol.

Both exchanges publish the same SEBI-mandated 4-level taxonomy:

    Macro-Economic Sector  (12 values)  e.g. Energy
      Sector               (22 values)  e.g. Oil, Gas & Consumable Fuels
        Industry           (~55 values) e.g. Petroleum Products
          Basic Industry   (~152)       e.g. Refineries & Marketing

Output (data/sector_classification.csv):
    symbol, isin, macro_sector, sector, industry, basic_industry,
    macro_code, sector_code, industry_code, basic_industry_code, source, as_of

Resolution precedence per symbol (first hit wins):
  1. reference/sector_overrides.csv — hand-curated, highest precedence.
  2. Cached screener.in HTML — the peer-comparison breadcrumb carries all four
     levels plus the official NSE hierarchy codes (IN03/IN0301/...). Free: the
     cache is already populated by fetch_financials.
  3. BSE ComHeader API — same four levels as Sector/IndustryNew/IGroup/ISubGroup,
     keyed by BSE scrip code resolved from the bulk scrip master via ISIN, then
     via BSE ticker. Recovers a handful of delisted names; no hierarchy codes.
  4. NSE index constituent file — Sector level only, current constituents only.

NSE's own `ind_niftytotalmarket_list.csv` is also used as a cross-check against
whichever source won, and disagreements are logged (they indicate a
reclassification that screener/BSE has not picked up yet).

The classification is a CURRENT snapshot, not point-in-time. No free historical
source for Indian sector classification exists, so a 2018 backtest is grouped by
today's labels. Reclassifications are rare; the `as_of` column records when the
snapshot was taken.

Run:
    python -m pipeline.fetch_sectors [--symbols A,B,C] [--force] [--offline]
"""

import argparse
import html as html_lib
import io
import json
import os
import re
from datetime import date
from typing import Optional

import pandas as pd

from pipeline.configs import config
from .utils.logger import get_logger
from .utils.http import make_session, polite_sleep
from .utils.cli import parse_symbol_list
from .utils.io import path
from .utils.gaps import log_gap

logger = get_logger("fetch_sectors")

OUT_FILE = "sector_classification.csv"

OUT_COLS = [
    "symbol", "isin",
    "macro_sector", "sector", "industry", "basic_industry",
    "macro_code", "sector_code", "industry_code", "basic_industry_code",
    "source", "as_of",
]

LEVELS = ["macro_sector", "sector", "industry", "basic_industry"]
UNKNOWN = "UNKNOWN"

# ---------------------------------------------------------------------------
# Source URLs
# ---------------------------------------------------------------------------
BSE_MASTER_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status={status}"
)
BSE_MASTER_STATUSES = ["Active", "Delisted", "Suspended"]
BSE_COMHEADER_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w"
    "?quotetype=EQ&scripcode={code}&seriesid="
)
BSE_HEADERS = {
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/json",
}

# Broadest NSE index constituent list (~750 names). `Industry` column == Sector level.
NSE_TOTALMARKET_URL = (
    "https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv"
)

# ---------------------------------------------------------------------------
# Canonical taxonomy
# ---------------------------------------------------------------------------

# The 12 macro-economic sectors and 22 sectors of the NSE/BSE unified taxonomy.
# Used as a drift tripwire: an unrecognised value means either a typo in an
# override or a genuine taxonomy revision worth a manual look.
KNOWN_MACRO = {
    "Commodities", "Consumer Discretionary", "Diversified", "Energy",
    "Fast Moving Consumer Goods", "Financial Services", "Healthcare",
    "Industrials", "Information Technology", "Services", "Telecommunication",
    "Utilities",
}
KNOWN_SECTORS = {
    "Automobile and Auto Components", "Capital Goods", "Chemicals",
    "Construction", "Construction Materials", "Consumer Durables",
    "Consumer Services", "Diversified", "Fast Moving Consumer Goods",
    "Financial Services", "Forest Materials", "Healthcare",
    "Information Technology", "Media, Entertainment & Publication",
    "Metals & Mining", "Oil, Gas & Consumable Fuels", "Power", "Realty",
    "Services", "Telecommunication", "Textiles", "Utilities",
}

# NSE's index CSVs drop the commas that NSE's own website (and BSE, and screener)
# keep. Normalise onto the comma-bearing spelling.
_ALIASES = {
    "Oil Gas & Consumable Fuels": "Oil, Gas & Consumable Fuels",
    "Media Entertainment & Publication": "Media, Entertainment & Publication",
    "Automobile and Auto components": "Automobile and Auto Components",
    "Information Technology ": "Information Technology",
}


def _clean(value: Optional[str]) -> str:
    """Unescape entities, collapse whitespace, apply spelling aliases."""
    if value is None:
        return ""
    s = re.sub(r"\s+", " ", html_lib.unescape(str(value))).strip()
    return _ALIASES.get(s, s)


# ---------------------------------------------------------------------------
# Source 1 — hand-curated overrides
# ---------------------------------------------------------------------------

def load_overrides() -> dict:
    """symbol -> {level: value}. Empty dict if the reference file is absent."""
    if not os.path.exists(config.SECTOR_OVERRIDES_FILE):
        logger.warning(f"no override file at {config.SECTOR_OVERRIDES_FILE}")
        return {}
    df = pd.read_csv(config.SECTOR_OVERRIDES_FILE)
    out = {}
    for row in df.to_dict("records"):
        sym = _clean(row.get("symbol")).upper()
        if not sym:
            continue
        out[sym] = {lvl: _clean(row.get(lvl)) for lvl in LEVELS}
    logger.info(f"loaded {len(out)} sector overrides")
    return out


# ---------------------------------------------------------------------------
# Source 2 — cached screener.in HTML
# ---------------------------------------------------------------------------

# The peer-comparison breadcrumb. Screener's `title` attributes name the levels
# one notch coarser than NSE does ("Broad Sector" is NSE's Macro-Economic Sector,
# screener's "Industry" is NSE's Basic Industry).
_BREADCRUMB_RE = re.compile(
    r'<a\s+href="/market/(IN[A-Z0-9/]*)"[^>]*?title="'
    r'(Broad Sector|Sector|Broad Industry|Industry)">([^<]*)</a>',
    re.DOTALL,
)

_SCREENER_LEVEL = {
    "Broad Sector":   ("macro_sector", "macro_code"),
    "Sector":         ("sector", "sector_code"),
    "Broad Industry": ("industry", "industry_code"),
    "Industry":       ("basic_industry", "basic_industry_code"),
}


def parse_screener_classification(page: str) -> Optional[dict]:
    """Extract the 4 levels + NSE hierarchy codes from a screener company page."""
    matches = _BREADCRUMB_RE.findall(page)
    if not matches:
        return None
    out = {}
    for code_path, title, value in matches:
        value_key, code_key = _SCREENER_LEVEL[title]
        out[value_key] = _clean(value)
        # href is /market/IN03/IN0301/... — the last segment is this level's code.
        out[code_key] = code_path.rstrip("/").rsplit("/", 1)[-1]
    return out if out.get("sector") else None


def screener_classification(symbol: str, offline: bool) -> Optional[dict]:
    """
    Classification from the screener cache, downloading the page only when there
    is no cached copy at all.

    Cache *age* is deliberately ignored here. fetch_financials owns screener cache
    freshness (SCREENER_CACHE_DAYS) and runs immediately before this step in the
    pipeline; a stale page still carries a perfectly good classification, and
    re-fetching one costs a multi-minute tenacity backoff when the page is gone.
    """
    cache_path = os.path.join(config.SCREENER_CACHE_DIR, f"{symbol}.html")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            parsed = parse_screener_classification(f.read())
        if parsed:
            return parsed
        # Page is cached but unclassified (typical for delisted names) — a
        # re-fetch would return the same thing. Fall through to the next source.
        return None
    if offline:
        return None
    from .fetch_financials import fetch_company_page
    page = fetch_company_page(symbol)
    return parse_screener_classification(page) if page else None


# ---------------------------------------------------------------------------
# Source 3 — BSE
# ---------------------------------------------------------------------------

_BSE_LEVEL_KEYS = [
    ("macro_sector", "Sector"),
    ("sector", "IndustryNew"),
    ("industry", "IGroup"),
    ("basic_industry", "ISubGroup"),
]


def load_bse_master(session, force: bool = False) -> dict:
    """
    BSE equity scrip master across Active/Delisted/Suspended, cached as JSON.
    Returns two lookups merged into one dict: ISIN -> row and BSE ticker -> row.
    """
    cache = config.BSE_MASTER_CACHE_FILE
    rows = None
    if not force and os.path.exists(cache):
        age = (date.today() - date.fromtimestamp(os.path.getmtime(cache))).days
        if age <= config.BSE_MASTER_CACHE_DAYS:
            with open(cache, encoding="utf-8") as f:
                rows = json.load(f)
            logger.debug(f"BSE scrip master: using cache ({age}d old, {len(rows)} rows)")

    if rows is None:
        rows = []
        for status in BSE_MASTER_STATUSES:
            try:
                r = session.get(BSE_MASTER_URL.format(status=status),
                                headers=BSE_HEADERS, timeout=120)
                r.raise_for_status()
                batch = r.json()
                rows.extend(batch)
                logger.info(f"BSE scrip master [{status}]: {len(batch)} rows")
            except Exception as e:
                logger.warning(f"BSE scrip master [{status}] failed: {e}")
            polite_sleep(config.MIN_SLEEP_NSE, config.MAX_SLEEP_NSE)
        if not rows:
            return {}
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(rows, f)

    lookup = {}
    for row in rows:
        for key in ((row.get("ISIN_NUMBER") or "").strip(),
                    (row.get("scrip_id") or "").strip().upper()):
            if key:
                lookup.setdefault(key, row)
    return lookup


def fetch_bse_classification(session, symbol: str, isin: str, master: dict) -> Optional[dict]:
    """Resolve a BSE scrip code (ISIN first, then ticker) and read its ComHeader."""
    row = master.get((isin or "").strip()) or master.get(symbol.upper())
    if row is None:
        return None
    code = str(row.get("SCRIP_CD") or "").strip()
    if not code:
        return None
    try:
        r = session.get(BSE_COMHEADER_URL.format(code=code),
                        headers=BSE_HEADERS, timeout=60)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.warning(f"{symbol}: BSE ComHeader (scrip {code}) failed: {e}")
        return None
    finally:
        polite_sleep(config.MIN_SLEEP_NSE, config.MAX_SLEEP_NSE)

    out = {lvl: _clean(payload.get(key)) for lvl, key in _BSE_LEVEL_KEYS}
    # BSE returns the keys with empty strings for most delisted scrips.
    return out if out.get("sector") else None


# ---------------------------------------------------------------------------
# Source 4 / cross-check — NSE index constituents
# ---------------------------------------------------------------------------

def load_nse_sector_map(session) -> dict:
    """symbol -> Sector, from NSE's broadest index constituent file."""
    try:
        r = session.get(NSE_TOTALMARKET_URL,
                        headers={"Referer": "https://www.nseindia.com/"}, timeout=60)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
    except Exception as e:
        logger.warning(f"NSE constituent list failed: {e}")
        return {}
    if "Symbol" not in df.columns or "Industry" not in df.columns:
        logger.warning(f"NSE constituent list schema changed: {list(df.columns)}")
        return {}
    out = {str(s).strip().upper(): _clean(i)
           for s, i in zip(df["Symbol"], df["Industry"]) if pd.notna(i)}
    logger.info(f"NSE constituent list: {len(out)} symbols (cross-check source)")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _blank_record(symbol: str, isin: str) -> dict:
    rec = {c: "" for c in OUT_COLS}
    rec["symbol"] = symbol
    rec["isin"] = isin
    for lvl in LEVELS:
        rec[lvl] = UNKNOWN
    rec["source"] = "unresolved"
    return rec


def resolve_all(meta: pd.DataFrame, offline: bool, force: bool) -> pd.DataFrame:
    overrides = load_overrides()
    today = date.today().isoformat()

    session = None
    bse_master = None
    nse_map = {}
    if not offline:
        session = make_session(warmup=False)
        nse_map = load_nse_sector_map(session)

    records = []
    for row in meta.to_dict("records"):
        symbol = str(row["symbol"]).strip().upper()
        isin = "" if pd.isna(row.get("isin")) else str(row.get("isin")).strip()
        rec = _blank_record(symbol, isin)

        resolved, source = None, None

        if symbol in overrides:
            resolved, source = overrides[symbol], "override"

        if resolved is None:
            parsed = screener_classification(symbol, offline)
            if parsed:
                resolved, source = parsed, "screener"

        if resolved is None and session is not None:
            if bse_master is None:
                bse_master = load_bse_master(session, force=force)
            parsed = fetch_bse_classification(session, symbol, isin, bse_master)
            if parsed:
                resolved, source = parsed, "bse"

        if resolved is None and symbol in nse_map:
            resolved = {lvl: UNKNOWN for lvl in LEVELS}
            resolved["sector"] = nse_map[symbol]
            source = "nse_bulk"

        if resolved is None:
            logger.warning(f"{symbol}: no classification from any source")
            log_gap("fetch_sectors", symbol, "sector_unresolved")
            records.append(rec)
            continue

        for key, value in resolved.items():
            if key in rec and value:
                rec[key] = value
        rec["source"] = source
        rec["as_of"] = today
        records.append(rec)

    df = pd.DataFrame(records, columns=OUT_COLS)

    # Cross-check whatever won against NSE's own published Sector.
    disagreements = [
        (r["symbol"], r["sector"], nse_map[r["symbol"]])
        for r in records
        if r["symbol"] in nse_map and r["sector"] not in (UNKNOWN, nse_map[r["symbol"]])
    ]
    for symbol, ours, theirs in disagreements:
        logger.warning(f"{symbol}: sector disagrees with NSE — ours={ours!r} nse={theirs!r}")
    if nse_map:
        checked = sum(1 for r in records if r["symbol"] in nse_map)
        logger.info(f"NSE cross-check: {checked - len(disagreements)}/{checked} agree")

    return df


def _log_coverage(df: pd.DataFrame) -> None:
    logger.info(f"resolved {len(df)} symbols")
    for source, n in df["source"].value_counts().items():
        logger.info(f"  source={source:<12} {n}")
    unknown = df[df["sector"] == UNKNOWN]
    logger.info(f"  UNKNOWN sector: {len(unknown)}")
    if not unknown.empty:
        logger.info(f"    {', '.join(unknown['symbol'].tolist())}")

    for level, known in (("macro_sector", KNOWN_MACRO), ("sector", KNOWN_SECTORS)):
        seen = set(df[level].dropna().unique()) - {UNKNOWN, ""}
        unexpected = seen - known
        if unexpected:
            logger.warning(f"{level}: values outside the known taxonomy: {sorted(unexpected)}")
        logger.info(f"  distinct {level}: {len(seen)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated subset (default: all metadata symbols).")
    parser.add_argument("--force", action="store_true",
                        help="Re-download the BSE scrip master regardless of cache age.")
    parser.add_argument("--offline", action="store_true",
                        help="Use local caches only; make no network requests.")
    args = parser.parse_args()

    meta_path = path("metadata.csv")
    if not os.path.exists(meta_path):
        raise SystemExit(f"metadata.csv not found at {meta_path}. Run `python -m pipeline.universe` first.")
    meta = pd.read_csv(meta_path)

    symbols = parse_symbol_list(args.symbols)
    if symbols:
        meta = meta[meta["symbol"].str.upper().isin(symbols)]
        if meta.empty:
            raise SystemExit(f"none of {symbols} found in metadata.csv")

    logger.info(f"=== fetch_sectors: {len(meta)} symbols (offline={args.offline}) ===")
    df = resolve_all(meta, offline=args.offline, force=args.force)

    out_path = path(OUT_FILE)
    if symbols and os.path.exists(out_path):
        # Subset run: merge into the existing file rather than truncating it.
        existing = pd.read_csv(out_path, dtype=str).fillna("")
        df = pd.concat([existing, df], ignore_index=True)
        df = df.drop_duplicates(subset=["symbol"], keep="last")

    df = df[OUT_COLS].sort_values("symbol").reset_index(drop=True)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    df.to_csv(out_path, index=False)

    _log_coverage(df)
    logger.info(f"wrote {len(df)} rows -> {out_path}")


if __name__ == "__main__":
    main()
