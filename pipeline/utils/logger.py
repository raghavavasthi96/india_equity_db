import logging
import os
import sys
from datetime import datetime

from pipeline.configs import config

# Single log file per process. Computed at module-load time so every get_logger()
# call within one `python script.py` invocation writes to the same file.
_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_PATH = os.path.join(config.LOG_DIR, f"run_{_RUN_TS}.log")
_FILE_HANDLER: logging.FileHandler | None = None


def _shared_file_handler(fmt: logging.Formatter) -> logging.FileHandler:
    global _FILE_HANDLER
    if _FILE_HANDLER is None:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        _FILE_HANDLER = logging.FileHandler(_LOG_PATH, encoding="utf-8")
        _FILE_HANDLER.setLevel(logging.DEBUG)
        _FILE_HANDLER.setFormatter(fmt)
    return _FILE_HANDLER


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    if hasattr(sh.stream, "reconfigure"):
        sh.stream.reconfigure(errors="replace")

    logger.addHandler(_shared_file_handler(fmt))
    logger.addHandler(sh)
    return logger
