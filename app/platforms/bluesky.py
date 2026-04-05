"""
Bluesky fetcher using the public AT Protocol API.

URL formats handled:
  https://bsky.app/profile/<handle>/post/<rkey>
  https://bsky.app/profile/<did>/post/<rkey>
"""

import re
from typing import Optional

import httpx

from .base import MediaInfo

_BSKY_API = "https://public.api.bsky.app/xrpc"
_BSKY_CDN = "https://cdn.bsky.app"
_URL_RE = re.compile(
    r"https?://bsky\.app/profile/(?P<handle>[^/]+)/post/(?P<rkey>[A-Za-z0-9]+)"
)


async def fetch(url: str) -> Optional[MediaInfo]:
    m = _URL_RE.match(url)
    if not m:
        return None

    handle, rkey = m.group("handle"), m.group("rkey")

    async with httpx.AsyncClient(timeout=10) as client:
        # Resolve handle → DID (skip if already a DID)
        if handle.startswith("did:"):
            did = handle
        else:
            resp = await client.get(
                f"{_BSKY_API}/com.atproto.identity.resolveHandle",
                params={"handle": handle},
            )
            resp.raise_for_status()
            did = resp.json()["did"]

        at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"

        resp = await client.get(
            f"{_BSKY_API}/app.bsky.feed.getPosts",
            params={"uris": at_uri},
        )
        resp.raise_for_status()
        posts = resp.json().get("posts", [])
        if not posts:
            return None

        post = posts[0]
        record = post.get("record", {})
        author = post.get("author", {})

        display_name = author.get("displayName") or author.get("handle", "")
        author_handle = author.get("handle", "")
        text = record.get("text", "")

        video_url: Optional[str] = None
        thumbnail_url: Optional[str] = None

        embed = record.get("embed", {})
        embed_type = embed.get("$type", "")

        if embed_type == "app.bsky.embed.video":
            video_blob = embed.get("video", {})
            cid = video_blob.get("ref", {}).get("$link") or video_blob.get("cid")
            if cid:
                # Direct blob endpoint — serves the raw video file (MP4)
                video_url = (
                    f"https://bsky.social/xrpc/com.atproto.sync.getBlob"
                    f"?did={did}&cid={cid}"
                )
                # Thumbnail from the post's view embed if present
                view_embed = post.get("embed", {})
                thumbnail_url = view_embed.get("thumbnail")

        elif embed_type == "app.bsky.embed.images":
            images = embed.get("images", [])
            if images:
                img_blob = images[0].get("image", {})
                cid = img_blob.get("ref", {}).get("$link") or img_blob.get("cid")
                if cid:
                    thumbnail_url = (
                        f"{_BSKY_CDN}/img/feed_thumbnail/plain/{did}/{cid}@jpeg"
                    )

        # Fallback thumbnail from author avatar
        if not thumbnail_url:
            thumbnail_url = author.get("avatar")

        return MediaInfo(
            url=url,
            title=text[:100] if text else f"Post by @{author_handle}",
            author=display_name or f"@{author_handle}",
            author_url=f"https://bsky.app/profile/{author_handle}",
            thumbnail_url=thumbnail_url,
            video_url=video_url,
            description=text,
            platform="Bluesky",
        )
