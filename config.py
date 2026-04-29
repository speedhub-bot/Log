"""
Centralised configuration — every tuneable comes from environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _str(key: str, default: str) -> str:
    return os.getenv(key, default)


# ── Telegram ────────────────────────────────────────────────
BOT_TOKEN: str = _str("BOT_TOKEN", "")
API_ID: int = _int("API_ID", 31206680)
API_HASH: str = _str("API_HASH", "")
ADMIN_ID: int = _int("ADMIN_ID", 5944410248)
SESSION_STRING: str = _str("SESSION_STRING", "")

# ── Processing ──────────────────────────────────────────────
MAX_CONCURRENT_JOBS: int = _int("MAX_CONCURRENT_JOBS", 2)
FREE_DAILY_LIMIT_GB: int = _int("FREE_DAILY_LIMIT_GB", 2)
FREE_DAILY_LIMIT_BYTES: int = FREE_DAILY_LIMIT_GB * 1024 ** 3
FREE_MAX_FILE_BYTES: int = 2 * 1024 ** 3          # 2 GB
VIP_MAX_FILE_BYTES: int = 10 * 1024 ** 3           # 10 GB

# ── Paths ───────────────────────────────────────────────────
DATABASE_PATH: str = _str("DATABASE_PATH", "bot.db")
LOG_FILE: str = _str("LOG_FILE", "bot.log")
TEMP_DIR: Path = Path(_str("TEMP_DIR", "/tmp/cookiebot"))

# ── Rate-limits / anti-abuse ────────────────────────────────
MAX_EXTRACTIONS_PER_HOUR: int = 3
SPAM_MSG_LIMIT: int = 10          # messages within window
SPAM_WINDOW_SECONDS: int = 30
SPAM_MUTE_SECONDS: int = 300      # 5 min
QUOTA_ABUSE_WARNS: int = 3        # auto-ban threshold

# ── Output ──────────────────────────────────────────────────
OUTPUT_CHUNK_SIZE_BYTES: int = 45 * 1024 * 1024    # 45 MB per result file
PROGRESS_UPDATE_INTERVAL: float = 3.0              # seconds
QUEUE_UPDATE_INTERVAL: float = 30.0                # seconds

# ── Cleanup ─────────────────────────────────────────────────
TEMP_FILE_MAX_AGE_HOURS: int = 1
