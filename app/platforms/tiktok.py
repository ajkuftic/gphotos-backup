"""
TikTok fetcher.

URL formats handled:
  https://www.tiktok.com/@<user>/video/<id>
  https://vm.tiktok.com/<short>
  https://vt.tiktok.com/<short>

Strategy:
  1. yt-dlp extracts the signed CDN video URL + thumbnail — no download.
  2. TikTok oEmbed fills in author/title if yt-dlp metadata is sparse.
  3. Fallback: oEmbed thumbnail + metadata card only.
"""

import asyncio
import re
from typing import Optional

import httpx
import yt_dlp

from .base import MediaInfo

_OEMBED = "https://www.tiktok.com/oembed"
_URL_RE = re.compile(
    r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/",
    re.IGNORECASE,
)


def _yt_extract(url: str) -> dict:
    """Run yt-dlp synchronously (called in a thread). Returns info dict."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Prefer a single MP4 format Slack can embed
        "format": "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


async def fetch(url: str) -> Optional[MediaInfo]:
    if not _URL_RE.match(url):
        return None

    # Run yt-dlp in a thread to keep the event loop free
    loop = asyncio.get_event_loop()
    info: dict = {}
    try:
        info = await loop.run_in_executor(None, _yt_extract, url)
    except Exception:
        pass

    # Also fetch oEmbed for reliable author/title (cheap, fast)
    oembed: dict = {}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(_OEMBED, params={"url": url})
            if resp.status_code == 200:
                oembed = resp.json()
    except Exception:
        pass

    if not info and not oembed:
        return None

    video_url: Optional[str] = info.get("url")
    thumbnail_url: Optional[str] = info.get("thumbnail") or oembed.get("thumbnail_url")
    title: str = (
        info.get("title")
        or oembed.get("title")
        or "TikTok video"
    )
    author: str = (
        info.get("uploader")
        or info.get("creator")
        or oembed.get("author_name")
        or ""
    )
    author_url: Optional[str] = (
        info.get("uploader_url") or oembed.get("author_url")
    )
    description: Optional[str] = info.get("description")

    return MediaInfo(
        url=url,
        title=title,
        author=author,
        author_url=author_url,
        thumbnail_url=thumbnail_url,
        video_url=video_url,
        description=description,
        platform="TikTok",
    )
