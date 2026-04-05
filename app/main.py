"""
Slackline — Slack link unfurler for TikTok, Instagram, and Bluesky.

Startup:
    uvicorn app.main:api --host 0.0.0.0 --port 8080

Required environment variables:
    SLACK_BOT_TOKEN      xoxb-…
    SLACK_SIGNING_SECRET …

Optional (Socket Mode local dev):
    SLACK_APP_TOKEN      xapp-…  (set this to skip HTTP mode)
"""

import asyncio
import logging
import os
from typing import Any, Optional

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from fastapi import FastAPI, Request, Response

from app.platforms import bluesky, instagram, tiktok
from app.platforms.base import MediaInfo
from app.slack.blocks import build_unfurl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Slack Bolt app
# ---------------------------------------------------------------------------

bolt = AsyncApp(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)

# ---------------------------------------------------------------------------
# Platform dispatch
# ---------------------------------------------------------------------------

_FETCHERS = [tiktok.fetch, instagram.fetch, bluesky.fetch]


async def _fetch_media(url: str) -> Optional[MediaInfo]:
    for fetcher in _FETCHERS:
        try:
            result = await fetcher(url)
            if result:
                return result
        except Exception as exc:
            logger.warning("Fetcher %s failed for %s: %s", fetcher.__module__, url, exc)
    return None


# ---------------------------------------------------------------------------
# link_shared handler
# ---------------------------------------------------------------------------

@bolt.event("link_shared")
async def handle_link_shared(event: dict[str, Any], client: Any) -> None:
    channel = event.get("channel")
    message_ts = event.get("message_ts")
    links: list[dict] = event.get("links", [])

    if not links:
        return

    # Fetch all URLs concurrently
    results = await asyncio.gather(
        *[_fetch_media(link["url"]) for link in links],
        return_exceptions=True,
    )

    unfurls: dict[str, Any] = {}
    for link, result in zip(links, results):
        if isinstance(result, MediaInfo):
            unfurls[link["url"]] = build_unfurl(result)
        elif isinstance(result, Exception):
            logger.warning("Fetch error for %s: %s", link["url"], result)

    if unfurls:
        await client.chat_unfurl(
            channel=channel,
            ts=message_ts,
            unfurls=unfurls,
        )


# ---------------------------------------------------------------------------
# FastAPI wrapper (HTTP mode)
# ---------------------------------------------------------------------------

api = FastAPI(title="Slackline")
handler = AsyncSlackRequestHandler(bolt)


@api.post("/slack/events")
async def slack_events(req: Request) -> Response:
    return await handler.handle(req)


@api.get("/health")
async def health() -> dict:
    return {"status": "ok"}
