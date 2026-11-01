import asyncio
import ipaddress
import json
import logging
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import yt_dlp

from config import config
from progress import DownloadCancelledError, ProgressTracker

logger = logging.getLogger(__name__)


@dataclass
class VideoResult:
    """Holds metadata and file paths for a downloaded video."""
    file_path: Path
    title: str
    duration: int
    width: int
    height: int
    file_size_bytes: int
    thumbnail_path: Optional[Path] = None


class DownloadError(Exception):
    """User-friendly download error exception."""
    pass


def parse_and_clean_txt(content: str) -> Tuple[List[str], int]:
    """
    Parse .txt content line by line.
    - Ignores empty lines and comment lines starting with '#'
    - Validates basic HTTP/HTTPS structure
    - Deduplicates while preserving order
    Returns: (unique_valid_urls, duplicate_count)
    """
    valid_urls: List[str] = []
    seen = set()
    duplicates = 0

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parsed = urlparse(line)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            continue

        if line in seen:
            duplicates += 1
            continue

        seen.add(line)
        valid_urls.append(line)

    return valid_urls, duplicates


def is_safe_public_url(url: str) -> Tuple[bool, str]:
    """
    SSRF Protection: Ensure the URL uses HTTP/HTTPS and resolves only to public,
    globally routable IP addresses (blocking localhost, LAN, link-local, and cloud metadata IPs).
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https"):
            return False, "Only HTTP and HTTPS schemes are allowed."

        hostname = parsed.hostname
        if not hostname:
            return False, "Missing hostname in URL."

        if hostname.lower() in ("localhost", "localhost.localdomain"):
            return False, "Localhost URLs are restricted."

        # Resolve hostname to IP addresses
        addr_info = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            ip_obj = ipaddress.ip_address(ip_str)
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
            ):
                return False, f"Access to internal/private IP ({ip_str}) is forbidden."

        return True, "OK"
    except socket.gaierror:
        return False, "Could not resolve domain name."
    except Exception as exc:
        return False, f"URL validation error: {exc}"


def probe_video_metadata(video_path: Path) -> Tuple[int, int, int]:
    """
    Use ffprobe to inspect video duration (seconds), width, and height.
    Returns (duration, width, height).
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json",
        str(video_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
        if result.returncode != 0:
            return 0, 0, 0

        data = json.loads(result.stdout or "{}")
        streams = data.get("streams", [])
        fmt = data.get("format", {})

        width = int(streams[0].get("width", 0)) if streams else 0
        height = int(streams[0].get("height", 0)) if streams else 0
        duration_float = float(fmt.get("duration", 0.0) or 0.0)

        return int(round(duration_float)), width, height
    except Exception as exc:
        logger.warning("ffprobe metadata extraction failed: %s", exc)
        return 0, 0, 0


def generate_thumbnail(video_path: Path, output_jpg: Path, duration: int) -> Optional[Path]:
    """
    Extract a frame using FFmpeg and scale it to fit within 320x320 (Telegram's
    thumbnail requirement) while preserving aspect ratio.
    """
    seek_seconds = max(1, min(5, duration // 5)) if duration > 2 else 0
    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(seek_seconds),
        "-i", str(video_path),
        "-vframes", "1",
        "-vf", "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
        "-q:v", "5",
        str(output_jpg),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
        if result.returncode == 0 and output_jpg.exists() and output_jpg.stat().st_size > 0:
            return output_jpg
    except Exception as exc:
        logger.warning("FFmpeg thumbnail generation failed: %s", exc)
    return None


def _sync_download_video(
    url: str,
    job_dir: Path,
    index: int,
    tracker: ProgressTracker,
) -> VideoResult:
    """
    Synchronous function designed to run inside asyncio.to_thread().
    Validates URL, invokes yt-dlp, enforces size limits, and generates thumbnail.
    """
    is_safe, reason = is_safe_public_url(url)
    if not is_safe:
        raise DownloadError(f"Blocked URL ({reason})")

    max_bytes = config.max_download_mb * 1024 * 1024
    target_mb = config.max_video_mb
    output_template = str(job_dir / f"video_{index:03d}.%(ext)s")

    # Prefer streams that fit within MAX_VIDEO_MB in MP4 format; fallback to best MP4
    format_selector = (
        f"bestvideo[filesize<{target_mb}M][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[filesize<{target_mb}M][ext=mp4]/"
        f"best[ext=mp4]/best"
    )

    ydl_opts = {
        "format": format_selector,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "max_filesize": max_bytes,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "nopart": False,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": False,
        "progress_hooks": [tracker.ytdlp_hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise DownloadError("No media information could be extracted from URL.")

            prepared_filename = ydl.prepare_filename(info)
            base_no_ext = os.path.splitext(prepared_filename)[0]
            mp4_candidate = Path(f"{base_no_ext}.mp4")
            actual_path = mp4_candidate if mp4_candidate.exists() else Path(prepared_filename)

            if not actual_path.exists():
                # Fallback search inside job_dir for matching prefix
                matches = list(job_dir.glob(f"video_{index:03d}.*"))
                matches = [
                    m for m in matches if m.suffix.lower() not in (".part", ".ytdl", ".jpg")
                ]
                if not matches:
                    raise DownloadError("Download finished, but output file was not found (possibly exceeded size limit).")
                actual_path = matches[0]

            file_size = actual_path.stat().st_size
            max_upload_bytes = config.max_video_mb * 1024 * 1024
            if file_size > max_upload_bytes:
                actual_path.unlink(missing_ok=True)
                raise DownloadError(
                    f"Downloaded file ({file_size / (1024 * 1024):.1f} MB) exceeds Telegram upload limit ({config.max_video_mb} MB)."
                )

            title = str(info.get("title") or f"Video {index}").strip()
            duration = int(info.get("duration") or 0)
            width = int(info.get("width") or 0)
            height = int(info.get("height") or 0)

            # Probe with ffprobe if metadata is missing or incomplete
            p_dur, p_w, p_h = probe_video_metadata(actual_path)
            if duration <= 0:
                duration = p_dur
            if width <= 0 or height <= 0:
                width, height = p_w, p_h

            thumb_path = job_dir / f"thumb_{index:03d}.jpg"
            generated_thumb = generate_thumbnail(actual_path, thumb_path, duration)

            return VideoResult(
                file_path=actual_path,
                title=title,
                duration=duration,
                width=width,
                height=height,
                file_size_bytes=file_size,
                thumbnail_path=generated_thumb,
            )

    except DownloadCancelledError:
        raise
    except yt_dlp.utils.DownloadError as exc:
        clean_msg = str(exc).split("\n")[-1].replace("ERROR:", "").strip()
        raise DownloadError(clean_msg or "Unsupported or unavailable video URL.") from exc
    except Exception as exc:
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError(f"Unexpected download failure: {exc}") from exc


async def download_video_async(
    url: str,
    job_dir: Path,
    index: int,
    tracker: ProgressTracker,
) -> VideoResult:
    """Run the blocking yt-dlp + FFmpeg workflow in a separate background thread."""
    return await asyncio.to_thread(_sync_download_video, url, job_dir, index, tracker)

