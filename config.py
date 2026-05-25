import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")

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
