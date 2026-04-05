# Slackline

Slack app that unfurls TikTok, Instagram, and Bluesky links with inline video playback — similar to how VxTiktok and FxInstagram work for Discord.

## How it works

1. A user pastes a TikTok, Instagram, or Bluesky link in any channel the bot is in.
2. Slack fires a `link_shared` event to this service.
3. The service extracts the direct video URL (via yt-dlp for TikTok/Instagram, AT Protocol for Bluesky) and returns a Slack `video` block.
4. Slack renders the video inline — no storage, no re-encoding, no cost per video.

Fallback chain: **video block → image card → text card**, depending on what the platform returns.

## Setup

### 1. Create the Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**
2. Paste the contents of `slack-manifest.yml`
3. Replace `YOUR_SERVICE_URL` with your deployed URL (see Deploy below)
4. Install the app to your workspace
5. Copy **Bot User OAuth Token** (`xoxb-…`) and **Signing Secret** from **Basic Information**

### 2. Configure environment

```bash
cp .env.example .env
# Fill in SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET
```

### 3. Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:api --reload --port 8080
```

Use [ngrok](https://ngrok.com) or [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) to expose port 8080 for Slack's event delivery.

### 4. Deploy to Cloud Run

```bash
gcloud run deploy slackline \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars SLACK_BOT_TOKEN=xoxb-...,SLACK_SIGNING_SECRET=...
```

Copy the service URL and update the `request_url` in your Slack app's **Event Subscriptions**.

### 5. Run with Docker Compose

```bash
docker compose up -d
```

## Architecture

```
app/
├── main.py               # FastAPI + Slack Bolt, link_shared handler
├── platforms/
│   ├── base.py           # MediaInfo dataclass
│   ├── bluesky.py        # AT Protocol API (no auth)
│   ├── tiktok.py         # yt-dlp + oEmbed
│   └── instagram.py      # yt-dlp
└── slack/
    └── blocks.py         # Block Kit builders (video / image card / text card)
```

## Supported URLs

| Platform | Formats |
|---|---|
| TikTok | `tiktok.com/@user/video/ID`, `vm.tiktok.com/…`, `vt.tiktok.com/…` |
| Instagram | `instagram.com/p/ID/`, `instagram.com/reel/ID/`, `instagram.com/tv/ID/` |
| Bluesky | `bsky.app/profile/handle/post/rkey` |

## Notes

- Video URLs from TikTok/Instagram are signed CDN links that expire after several hours. This is fine — Slack renders the unfurl immediately and caches it.
- Private/age-restricted posts on Instagram will fall back to a text card.
- Bluesky video blobs are served directly from `bsky.social` with no expiry.
