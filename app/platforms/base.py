from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MediaInfo:
    """Normalised media metadata returned by every platform fetcher."""

    url: str                          # Original URL
    title: str
    author: str
    author_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    video_url: Optional[str] = None   # Direct MP4/MOV URL; None → image-card fallback
    description: Optional[str] = None
    platform: str = ""
