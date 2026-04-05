"""
Instagram fetcher.

URL formats handled:
  https://www.instagram.com/p/<id>/
  https://www.instagram.com/reel/<id>/
  https://www.instagram.com/tv/<id>/

Strategy: yt-dlp URL extraction only — no Meta API token required.
Falls back to a minimal card if yt-dlp can't access the post
(private accounts, age-restricted content, etc.).
"""

import asyncio
import re
from typing import Optional

import yt_dlp

from .base import MediaInfo

_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)


def _yt_extract(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


async def fetch(url: str) -> Optional[MediaInfo]:
    if not _URL_RE.match(url):
        return None

    loop = asyncio.get_event_loop()
    info: dict = {}
    try:
        info = await loop.run_in_executor(None, _yt_extract, url)
    except Exception:
        pass

    if not info:
        return None

    # yt-dlp may return a playlist for carousels; take the first entry
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        info = entries[0] if entries else {}

    if not info:
        return None

    video_url: Optional[str] = info.get("url")
    # For image posts yt-dlp won't have a video url but will have a thumbnail
    thumbnail_url: Optional[str] = info.get("thumbnail")
    title: str = info.get("title") or info.get("description") or "Instagram post"
    # Truncate long captions used as title
    if len(title) > 120:
        title = title[:117] + "..."
    author: str = info.get("uploader") or info.get("channel") or ""
    author_url: Optional[str] = info.get("uploader_url") or info.get("channel_url")
    description: Optional[str] = info.get("description")

    return MediaInfo(
        url=url,
        title=title,
        author=author,
        author_url=author_url,
        thumbnail_url=thumbnail_url,
        video_url=video_url,
        description=description,
        platform="Instagram",
    )
