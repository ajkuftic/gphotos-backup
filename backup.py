#!/usr/bin/env python3
"""
Google Photos Backup
Downloads full-quality photos and videos from your library and shared albums.

Directory layout on disk:
  /data/photos/YYYY/MM/<filename>

State is tracked in /data/backup_state.json so interrupted runs resume cleanly.
"""

import json
import logging
import os
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly"]
API_BASE = "https://photoslibrary.googleapis.com/v1"

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
AUTH_PORT = int(os.environ.get("AUTH_PORT", "8080"))
TOKEN_FILE = CONFIG_DIR / "token.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
STATE_FILE = DATA_DIR / "backup_state.json"
PHOTOS_DIR = DATA_DIR / "photos"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate() -> Credentials:
    """Return valid Google API credentials, triggering OAuth flow if needed."""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing access token…")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                log.error(
                    "OAuth credentials not found at %s\n"
                    "Please follow the setup instructions in README.md to create "
                    "OAuth 2.0 credentials in Google Cloud Console and download "
                    "the JSON file to /config/credentials.json.",
                    CREDENTIALS_FILE,
                )
                sys.exit(1)

            log.info(
                "Starting OAuth flow.\n"
                "  1. Open the URL printed below in your browser.\n"
                "  2. Authorize the application.\n"
                "  3. The browser will redirect to http://localhost:8080 — "
                "make sure port 8080 is forwarded from the container to your host.\n"
            )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(
                host="localhost",      # used for redirect_uri sent to Google
                bind_addr="0.0.0.0",  # bind all interfaces so Docker port-mapping works
                port=AUTH_PORT,
                open_browser=False,
                prompt="consent",
            )

        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json())
        log.info("Token saved to %s", TOKEN_FILE)

    return creds


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class PhotosClient:
    """Thin wrapper around the Google Photos Library REST API."""

    def __init__(self, creds: Credentials) -> None:
        self.creds = creds
        self.session = requests.Session()
        self._update_auth_header()

    def _update_auth_header(self) -> None:
        if self.creds.expired and self.creds.refresh_token:
            self.creds.refresh(Request())
        self.session.headers.update(
            {"Authorization": f"Bearer {self.creds.token}"}
        )

    def _request(self, method: str, url: str, **kwargs):
        for attempt in range(6):
            self._update_auth_header()
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt
                log.warning(
                    "HTTP %s — backing off %ds (attempt %d/6)",
                    resp.status_code, wait, attempt + 1,
                )
                time.sleep(wait)
                continue
            if not resp.ok:
                log.error(
                    "HTTP %s from %s\nResponse body: %s",
                    resp.status_code, url, resp.text,
                )
            resp.raise_for_status()
            return resp
        raise RuntimeError(f"Request failed after 6 attempts: {method} {url}")

    def get(self, path: str, **kwargs):
        return self._request("GET", f"{API_BASE}/{path}", **kwargs)

    def post(self, path: str, **kwargs):
        return self._request("POST", f"{API_BASE}/{path}", **kwargs)

    # -----------------------------------------------------------------------
    # Listing helpers
    # -----------------------------------------------------------------------

    def list_media_items(self):
        """Yield every media item in the authenticated user's library."""
        page_token = None
        while True:
            params = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            data = self.get("mediaItems", params=params).json()
            yield from data.get("mediaItems", [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    def list_shared_albums(self):
        """Yield every shared album visible to the authenticated user."""
        page_token = None
        while True:
            params = {"pageSize": 50}
            if page_token:
                params["pageToken"] = page_token
            data = self.get("sharedAlbums", params=params).json()
            yield from data.get("sharedAlbums", [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    def list_album_items(self, album_id: str):
        """Yield every media item in the given album."""
        page_token = None
        while True:
            body = {"albumId": album_id, "pageSize": 100}
            if page_token:
                body["pageToken"] = page_token
            data = self.post("mediaItems:search", json=body).json()
            yield from data.get("mediaItems", [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    def get_media_item(self, item_id: str) -> dict:
        """Fetch a fresh copy of a media item (baseUrl expires ~1 h)."""
        return self.get(f"mediaItems/{item_id}").json()


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _download_url(item: dict) -> str:
    """
    Return the URL that produces the original-quality file.

    Photos: baseUrl + "=d"   → original image bytes
    Videos: baseUrl + "=dv"  → original video bytes (not a thumbnail)
    """
    base = item["baseUrl"]
    if "video" in item.get("mediaMetadata", {}):
        return base + "=dv"
    return base + "=d"


def _local_path(item: dict) -> Path:
    """Return the target path under PHOTOS_DIR, organised YYYY/MM/filename."""
    creation_time = item.get("mediaMetadata", {}).get("creationTime", "")
    try:
        dt = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
        subfolder = dt.strftime("%Y/%m")
    except (ValueError, AttributeError):
        subfolder = "unknown"

    filename = item.get("filename") or item["id"]
    return PHOTOS_DIR / subfolder / filename


def _download_file(client: PhotosClient, item: dict, dest: Path) -> bool:
    """
    Stream the full-quality file to *dest*.
    Returns True on success, False on permanent failure.
    """
    # Re-fetch to get a fresh baseUrl (they expire after ~1 hour)
    fresh = client.get_media_item(item["id"])
    url = _download_url(fresh)

    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(4):
        try:
            with client.session.get(url, stream=True, timeout=300) as resp:
                resp.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=65536):
                        fh.write(chunk)
                tmp.rename(dest)
            return True
        except Exception as exc:
            if attempt == 3:
                log.error("Failed to download %s: %s", item.get("filename"), exc)
                if dest.with_suffix(dest.suffix + ".part").exists():
                    dest.with_suffix(dest.suffix + ".part").unlink()
                return False
            wait = 2 ** attempt
            log.warning("Download error (%s), retrying in %ds…", exc, wait)
            time.sleep(wait)

    return False


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("State file corrupt — starting fresh.")
    return {"downloaded": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


# ---------------------------------------------------------------------------
# Core backup logic
# ---------------------------------------------------------------------------

def _process_item(
    client: PhotosClient,
    item: dict,
    state: dict,
    dry_run: bool,
    counters: dict,
) -> None:
    item_id = item["id"]
    filename = item.get("filename", item_id)

    # Already downloaded and file still present?
    if item_id in state["downloaded"]:
        existing = Path(state["downloaded"][item_id])
        if existing.exists():
            log.debug("Skip (already downloaded): %s", filename)
            counters["skipped"] += 1
            return
        log.info("Re-downloading (file missing): %s", filename)

    dest = _local_path(item)

    # Handle filename collisions with already-downloaded different items
    if dest.exists() and item_id not in state["downloaded"]:
        dest = dest.with_stem(f"{dest.stem}_{item_id[:8]}")

    if dry_run:
        log.info("[DRY RUN] %s → %s", filename, dest)
        counters["skipped"] += 1
        return

    ok = _download_file(client, item, dest)
    if ok:
        state["downloaded"][item_id] = str(dest)
        counters["downloaded"] += 1
        if counters["downloaded"] % 25 == 0:
            _save_state(state)
            log.info(
                "Progress — downloaded: %d, skipped: %d, errors: %d",
                counters["downloaded"], counters["skipped"], counters["errors"],
            )
    else:
        counters["errors"] += 1


def backup(args: argparse.Namespace) -> None:
    log.info("Authenticating with Google Photos…")
    creds = authenticate()
    client = PhotosClient(creds)

    state = _load_state()
    counters = {"downloaded": 0, "skipped": 0, "errors": 0}

    def process(item):
        try:
            _process_item(client, item, state, args.dry_run, counters)
        except Exception as exc:
            log.error(
                "Unexpected error for %s: %s", item.get("filename", item["id"]), exc
            )
            counters["errors"] += 1

    # -----------------------------------------------------------------------
    # Own library
    # -----------------------------------------------------------------------
    if not args.shared_only:
        log.info("Scanning your library…")
        for item in client.list_media_items():
            process(item)

    # -----------------------------------------------------------------------
    # Shared albums (albums shared *with* the authenticated user)
    # -----------------------------------------------------------------------
    if args.include_shared or args.shared_only:
        log.info("Scanning shared albums…")
        seen_album_ids: set[str] = set()
        for album in client.list_shared_albums():
            album_id = album["id"]
            if album_id in seen_album_ids:
                continue
            seen_album_ids.add(album_id)
            title = album.get("title", album_id)
            log.info("  Album: %s", title)
            for item in client.list_album_items(album_id):
                process(item)

    _save_state(state)
    log.info(
        "Done — downloaded: %d, skipped: %d, errors: %d",
        counters["downloaded"], counters["skipped"], counters["errors"],
    )
    if counters["errors"]:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Back up full-quality photos and videos from Google Photos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # First-time auth (map port 8080 so the OAuth redirect reaches the container)
  docker run --rm -p 8080:8080 -v ./config:/config gphotos-backup --auth-only

  # Backup your own library
  docker run --rm -v ./config:/config -v ./data:/data gphotos-backup

  # Backup your library AND all albums shared with you
  docker run --rm -v ./config:/config -v ./data:/data gphotos-backup --include-shared

  # Backup only shared albums
  docker run --rm -v ./config:/config -v ./data:/data gphotos-backup --shared-only
""",
    )
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Authenticate and save token, then exit (no download). "
             "Requires -p 8080:8080.",
    )
    parser.add_argument(
        "--include-shared",
        action="store_true",
        help="Also download from albums shared with you.",
    )
    parser.add_argument(
        "--shared-only",
        action="store_true",
        help="Only download from albums shared with you (skip own library).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be downloaded without actually downloading.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging.",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.include_shared and args.shared_only:
        parser.error("--include-shared and --shared-only are mutually exclusive.")

    if args.auth_only:
        log.info("Authenticating…")
        authenticate()
        log.info("Authentication complete.")
        return

    backup(args)


if __name__ == "__main__":
    main()
