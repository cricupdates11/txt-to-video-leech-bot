import os
import sys
import logging
from typing import Set
from dotenv import load_dotenv

# Load environment variables from .env file if present (for local development)
load_dotenv()

logger = logging.getLogger(__name__)


def _parse_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in ("true", "1", "yes", "on")


def _parse_int(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return default


def _parse_admin_ids(value: str) -> Set[int]:
    ids: Set[int] = set()
    if not value:
        return ids
    for part in value.split(","):
        part = part.strip()
        if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
            ids.add(int(part))
    return ids


class Config:
    """Centralized configuration and runtime state for the bot."""

    def __init__(self) -> None:
        self.bot_token: str = os.getenv("BOT_TOKEN", "").strip()
        if not self.bot_token or self.bot_token == "your_telegram_bot_token_here":
            logger.critical(
                "CRITICAL ERROR: BOT_TOKEN environment variable is missing or invalid! "
                "Please set BOT_TOKEN in your .env file or Render environment variables."
            )
            sys.exit(1)

        self.admin_ids: Set[int] = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
        self.max_urls: int = max(1, _parse_int(os.getenv("MAX_URLS", "10"), 10))
        self.max_video_mb: int = max(1, _parse_int(os.getenv("MAX_VIDEO_MB", "49"), 49))
        self.max_download_mb: int = max(
            self.max_video_mb, _parse_int(os.getenv("MAX_DOWNLOAD_MB", "500"), 500)
        )
        self.max_concurrent_jobs: int = max(
            1, _parse_int(os.getenv("MAX_CONCURRENT_JOBS", "2"), 2)
        )
        self.private_mode: bool = _parse_bool(os.getenv("PRIVATE_MODE", "false"), False)

        # Runtime state (modifiable via Admin commands)
        self.authorized_users: Set[int] = set(self.admin_ids)
        self.caption_template: str = (
            "🎬 <b>Video Name:</b> {title}\n"
            "⏱ <b>Duration:</b> {duration}\n"
            "📦 <b>File Size:</b> {size}\n"
            "🤖 <b>Processed By:</b> TXT Leech Bot"
        )

        # Basic runtime statistics
        self.total_jobs_processed: int = 0
        self.total_videos_uploaded: int = 0
        self.total_Download_failures: int = 0

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def is_authorized(self, user_id: int) -> bool:
        if not self.private_mode:
            return True
        return user_id in self.admin_ids or user_id in self.authorized_users


# Singleton instance imported across modules
config = Config()

