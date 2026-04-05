"""
Build Slack unfurl payloads from MediaInfo objects.

Priority:
  1. video block  — when we have a direct video URL + thumbnail
  2. image card   — when we have a thumbnail but no video URL
  3. text card    — title + author only (last resort)
"""

from typing import Any

from app.platforms.base import MediaInfo

_PLATFORM_ICONS = {
    "TikTok": "https://www.tiktok.com/favicon.ico",
    "Instagram": "https://www.instagram.com/favicon.ico",
    "Bluesky": "https://bsky.app/favicon.ico",
}


def build_unfurl(info: MediaInfo) -> dict[str, Any]:
    """Return a single unfurl value (Block Kit blocks list)."""
    if info.video_url and info.thumbnail_url:
        return _video_unfurl(info)
    if info.thumbnail_url:
        return _image_card_unfurl(info)
    return _text_card_unfurl(info)


def _footer_context(info: MediaInfo) -> dict[str, Any]:
    icon = _PLATFORM_ICONS.get(info.platform)
    elements: list[dict] = []
    if icon:
        elements.append({"type": "image", "image_url": icon, "alt_text": info.platform})
    elements.append(
        {
            "type": "mrkdwn",
            "text": (
                f"*{info.platform}* · "
                + (f"<{info.author_url}|{info.author}>" if info.author_url else info.author)
            ),
        }
    )
    return {"type": "context", "elements": elements}


def _video_unfurl(info: MediaInfo) -> dict[str, Any]:
    blocks: list[dict] = [
        {
            "type": "video",
            "video_url": info.video_url,
            "thumbnail_url": info.thumbnail_url,
            "alt_text": info.title,
            "title": {
                "type": "plain_text",
                "text": info.title[:2000],
            },
        },
    ]
    if info.description and info.description != info.title:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": info.description[:3000],
                },
            }
        )
    blocks.append(_footer_context(info))
    return {"blocks": blocks}


def _image_card_unfurl(info: MediaInfo) -> dict[str, Any]:
    blocks: list[dict] = [
        {
            "type": "image",
            "image_url": info.thumbnail_url,
            "alt_text": info.title,
            "title": {
                "type": "plain_text",
                "text": info.title[:2000],
            },
        },
    ]
    if info.description and info.description != info.title:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": info.description[:3000]},
            }
        )
    blocks.append(_footer_context(info))
    return {"blocks": blocks}


def _text_card_unfurl(info: MediaInfo) -> dict[str, Any]:
    author_text = (
        f"<{info.author_url}|{info.author}>" if info.author_url else info.author
    )
    text = f"*<{info.url}|{info.title}>*"
    if author_text:
        text += f"\n{author_text}"
    if info.description and info.description != info.title:
        text += f"\n{info.description[:500]}"
    return {
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            _footer_context(info),
        ]
    }
