import asyncio
import html
import logging
import time
from typing import Any, Dict
from telegram import Message
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TimedOut

logger = logging.getLogger(__name__)


class DownloadCancelledError(Exception):
    """Raised inside the yt-dlp progress hook when a user cancels the job."""
    pass


def format_bytes(num_bytes: float) -> str:
    """Convert raw bytes into a human-readable string (KB, MB, GB)."""
    if num_bytes <= 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def format_duration(seconds: int) -> str:
    """Convert seconds into MM:SS or HH:MM:SS format."""
    if seconds <= 0:
        return "00:00"
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


class ProgressTracker:
    """
    Bridges synchronous yt-dlp progress callbacks (running in a worker thread)
    with asynchronous Telegram status message updates on the main event loop.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        status_message: Message,
        current_index: int,
        total_urls: int,
        cancel_event: asyncio.Event,
        update_interval: float = 4.0,
    ) -> None:
        self.loop = loop
        self.status_message = status_message
        self.current_index = current_index
        self.total_urls = total_urls
        self.cancel_event = cancel_event
        self.update_interval = update_interval

        self.last_update_time: float = 0.0
        self.last_sent_text: str = ""
        self._updating_lock = False

    def ytdlp_hook(self, d: Dict[str, Any]) -> None:
        """
        Synchronous callback invoked by yt-dlp during download.
        Checks for cancellation immediately and schedules throttled UI updates.
        """
        if self.cancel_event.is_set():
            raise DownloadCancelledError("Job cancelled by user.")

        status = d.get("status")
        if status == "downloading":
            now = time.monotonic()
            if (now - self.last_update_time) < self.update_interval or self._updating_lock:
                return

            downloaded = d.get("downloaded_bytes", 0) or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            speed = d.get("speed") or 0.0

            if total > 0:
                percent = (downloaded / total) * 100.0
                percent_str = f"{percent:.1f}%"
                size_str = f"{format_bytes(downloaded)} / {format_bytes(total)}"
            else:
                percent_str = "Calculating..."
                size_str = f"{format_bytes(downloaded)} / Unknown"

            speed_str = f"{format_bytes(speed)}/s" if speed else "0 B/s"

            text = (
                f"⬇️ <b>Downloading:</b> {self.current_index}/{self.total_urls}\n"
                f"<b>Progress:</b> {html.escape(percent_str)}\n"
                f"<b>Speed:</b> {html.escape(speed_str)}\n"
                f"<b>Downloaded:</b> {html.escape(size_str)}"
            )

            if text != self.last_sent_text:
                self.last_update_time = now
                self._updating_lock = True
                asyncio.run_coroutine_threadsafe(
                    self._safe_edit_message(text), self.loop
                )

    async def _safe_edit_message(self, text: str) -> None:
        """Asynchronously edit the Telegram status message with flood protection."""
        try:
            if self.cancel_event.is_set():
                return
            await self.status_message.edit_text(text=text, parse_mode=ParseMode.HTML)
            self.last_sent_text = text
        except RetryAfter as e:
            # Respect Telegram flood limits by advancing last_update_time
            self.last_update_time = time.monotonic() + float(e.retry_after)
        except (BadRequest, TimedOut):
            # Ignore "Message is not modified" or transient network hiccups
            pass
        except Exception as exc:
            logger.debug("Minor error updating progress message: %s", exc)
        finally:
            self._updating_lock = False

