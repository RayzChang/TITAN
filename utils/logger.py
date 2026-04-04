"""
TITAN v1 — 日誌系統
控制台 + 每日檔案輸出，繁體中文訊息，保留 30 天
"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOGS_DIR = Path(__file__).parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

_initialized = False


def get_logger(name: str = "TITAN") -> logging.Logger:
    global _initialized
    logger = logging.getLogger(name)

    if _initialized:
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台輸出
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    # 檔案輸出（每日切換，保留 30 天）
    log_file = LOGS_DIR / "titan.log"
    file_handler = TimedRotatingFileHandler(
        log_file, when="midnight", interval=1, backupCount=30, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    _initialized = True

    return logger
