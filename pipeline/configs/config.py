import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_PKG_ROOT = os.path.dirname(os.path.dirname(__file__))  # pipeline/
DATA_DIR = os.path.join(_PKG_ROOT, "data")
LOG_DIR = os.path.join(_PKG_ROOT, "logs")

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
UNIVERSE_SIZE = 500
REBALANCE_FREQ = "Q"

# Single source of truth for the project-wide history floor. Every fetcher
# and the universe ranker MUST respect this. Pinned to the earliest XBRL
# filing date available from NSE; do not lower without re-validating XBRL
# coverage for the new range.
PROJECT_START_DATE = "2018-06-30"

# Prices-only history buffer fetched *before* PROJECT_START_DATE. Universe and
# XBRL still floor at PROJECT_START_DATE — only `prices_panel` (and downstream
# `close_wide` / `adv_30d_inr` caches) extend earlier. Sized to the longest
# signal lookback used by backtests (e.g. momentum_12_1 needs ~12mo of trailing
# closes at the first rebal date).
PRICE_HISTORY_LOOKBACK_DAYS = 365

# Financials-only history buffer kept *before* PROJECT_START_DATE. `financials_panel`
# and `financials_annual_panel` retain this many quarter-ends of prior data so YoY /
# multi-quarter signals (e.g. earnings_growth_yoy) have a "prior" reading available
# at the first rebal date. Sized to cover the longest fundamental lookback used by
# backtests (YoY = 4q + filing-lag buffer ≈ 5q; 6q gives one quarter of slack).
FINANCIALS_HISTORY_LOOKBACK_QUARTERS = 6

# ---------------------------------------------------------------------------
# HTTP politeness
# ---------------------------------------------------------------------------
MIN_SLEEP_SCREENER, MAX_SLEEP_SCREENER = 1.0, 2.0
MIN_SLEEP_NSE, MAX_SLEEP_NSE = 0.5, 1.0
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Parallelism
# ---------------------------------------------------------------------------
XBRL_WORKERS = 8
FINANCIALS_WORKERS = 8

# ---------------------------------------------------------------------------
# Caches (dirs + TTLs, grouped by source)
# ---------------------------------------------------------------------------
SCREENER_CACHE_DIR = os.path.join(DATA_DIR, "screener_cache")
SCREENER_CACHE_DAYS = 7

XBRL_CACHE_DIR = os.path.join(DATA_DIR, "xbrl_cache")
XBRL_INDEX_CACHE_DAYS = 1

BHAVCOPY_CACHE_DIR = os.path.join(DATA_DIR, "bhavcopy_cache")

NSE_CORPACT_CACHE_DIR = os.path.join(DATA_DIR, "nse_corpact_cache")
NSE_CORPACT_CACHE_DAYS = 7

# BSE scrip master (ISIN / scrip_id -> scrip code), used only by fetch_sectors as a
# fallback for symbols whose screener page carries no industry classification.
BSE_MASTER_CACHE_FILE = os.path.join(DATA_DIR, "bse_scrip_master.json")
BSE_MASTER_CACHE_DAYS = 7

# ---------------------------------------------------------------------------
# Reference data (tracked in git, unlike DATA_DIR)
# ---------------------------------------------------------------------------
REFERENCE_DIR = os.path.join(_PKG_ROOT, "reference")
SECTOR_OVERRIDES_FILE = os.path.join(REFERENCE_DIR, "sector_overrides.csv")
