#!/usr/bin/env python3
"""
Google Photos Backup via Playwright browser automation.

Uses the Google Photos web interface instead of the restricted Photos Library API.
The Chromium session is persisted in /config/browser-data between runs —
authenticate once on a machine with a display, then run headlessly anywhere.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests
from playwright.async_api import async_playwright, BrowserContext, Page

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BROWSER_DATA_DIR = CONFIG_DIR / "browser-data"
SESSION_FILE = CONFIG_DIR / "session.json"   # portable cross-platform session
STATE_FILE = DATA_DIR / "backup_state.json"
PHOTOS_DIR = DATA_DIR / "photos"

GPHOTOS_URL = "https://photos.google.com"
SHARING_URL = "https://photos.google.com/sharing"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",  "image/png": ".png",   "image/gif": ".gif",
    "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif",
    "image/tiff": ".tiff", "image/bmp": ".bmp",
    "video/mp4": ".mp4",   "video/quicktime": ".mov",
    "video/x-msvideo": ".avi", "video/webm": ".webm",
    "video/3gpp": ".3gp",  "video/mpeg": ".mpeg", "video/x-matroska": ".mkv",
}

# Flags required for Chromium running inside Docker (especially as root)
_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",  # avoids /dev/shm OOM crashes (64 MB Docker default)
    "--no-sandbox",             # required when running as root in Docker
    "--disable-gpu",
]


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

async def _open_context(pw, *, headless: bool) -> BrowserContext:
    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return await pw.chromium.launch_persistent_context(
        str(BROWSER_DATA_DIR),
        headless=headless,
        args=_CHROMIUM_ARGS,
        viewport={"width": 1280, "height": 900},
        accept_downloads=True,
    )


async def _is_signed_in(page: Page) -> bool:
    """Navigate to Google Photos and return True if already signed in."""
    await page.goto(GPHOTOS_URL, wait_until="domcontentloaded", timeout=30_000)
    # Wait briefly for any JS redirect to fire before checking the final URL.
    await page.wait_for_timeout(3000)
    url = page.url
    # Must be on the actual library host, not the marketing/about site or login page.
    return (
        url.startswith("https://photos.google.com")
        and "/about" not in url
        and "/login" not in url
        and "accounts.google.com" not in url
        and "ServiceLogin" not in url
        and "signin" not in url.lower()
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def do_auth() -> None:
    """
    Open a headed Chromium window for the user to sign in to Google Photos.
    The session (cookies + local storage) is saved to BROWSER_DATA_DIR and
    reused by subsequent headless backup runs.
    """
    log.info("Opening browser for sign-in…")
    log.info(
        "If this machine has no display, authenticate on your LOCAL machine instead:\n"
        "  docker compose run --rm gphotos-auth   (on local machine)\n"
        "  rsync -av ./config/browser-data/ user@server:/path/to/config/browser-data/\n"
        "  (then run the backup on the server headlessly)"
    )

    async with async_playwright() as pw:
        ctx = await _open_context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Start navigation — don't await networkidle; Google's JS redirect fires
        # asynchronously so we can't rely on a single wait_until value.
        await page.goto(GPHOTOS_URL, wait_until="domcontentloaded", timeout=30_000)

        # Give Google's JS a moment to trigger the sign-in redirect before we
        # start inspecting the URL, so we don't read "photos.google.com" before
        # the redirect has fired.
        await page.wait_for_timeout(3000)

        log.info("Please sign in to Google in the browser window (5-minute timeout)…")

        # Wait until we land on the signed-in Google Photos library.
        # We look for the main photo grid element rather than just the URL so we
        # know the page is fully ready (not just mid-redirect).
        try:
            await page.wait_for_function(
                """() => {
                    const url = location.href;
                    if (!url.includes('photos.google.com')) return false;
                    if (url.includes('accounts.google.com')) return false;
                    if (/signin|challenge|oauth|ServiceLogin/i.test(url)) return false;
                    // Confirm library content is present (photo grid or empty-state msg)
                    return !!(
                        document.querySelector('a[href*="/photo/"]') ||
                        document.querySelector('c-wiz[data-p]') ||
                        document.querySelector('[data-latest-bg]') ||
                        document.querySelector('[jscontroller][class*="photo"]')
                    );
                }""",
                timeout=300_000,  # 5 minutes
            )
        except Exception:
            log.error("Sign-in timed out.")
            await ctx.close()
            sys.exit(1)

        await asyncio.sleep(2)  # let session cookies settle

        # Export session as portable JSON so it can be transferred cross-platform
        # (macOS Keychain-encrypted cookies in browser-data/ can't be read on Linux).
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(SESSION_FILE))
        log.info("Signed in — session saved to %s and %s", BROWSER_DATA_DIR, SESSION_FILE)
        await ctx.close()


# ---------------------------------------------------------------------------
# Media extraction
# ---------------------------------------------------------------------------

# JavaScript injected into the page to extract visible media items.
# Returns [{cdnId, base, isVideo, href}] for every photo/video anchor found.
_EXTRACT_JS = """
() => {
    const results = [];
    const seen = new Set();

    for (const a of document.querySelectorAll('a[href*="/photo/"]')) {
        // Google Photos lazy-loads thumbnails — try every plausible source attribute
        // as well as the tile's data-latest-bg background-image attribute.
        let base = null;

        // Google Photos uses several CDN domains depending on account/region:
        //   lh3.googleusercontent.com  (legacy)
        //   photos.fife.usercontent.google.com  (current)
        // Accept any URL that contains either pattern.
        const isCdnUrl = u => u.includes('googleusercontent.com') ||
                               u.includes('usercontent.google.com');

        const img = a.querySelector('img');
        if (img) {
            const src = img.src
                     || img.getAttribute('data-src')
                     || img.getAttribute('data-iml')
                     || '';
            if (isCdnUrl(src)) {
                base = src.replace(/=[^/]*$/, '');
            }
        }

        // Fallback 1: inline background-image style on a child element inside <a>
        // (e.g. <div class="RY3tic" style="background-image: url('...')">)
        if (!base) {
            const inner = a.querySelector('[style*="background-image"]');
            if (inner) {
                const styleVal = inner.getAttribute('style') || '';
                const m = styleVal.match(/url\(["']?(https?:\/\/[^"')]+)/);
                if (m && isCdnUrl(m[1])) {
                    base = m[1].replace(/=[^/]*$/, '');
                }
            }
        }

        // Fallback 2: data-latest-bg on an ancestor element
        if (!base) {
            const tile = a.closest('[data-latest-bg]');
            if (tile) {
                const bg = tile.getAttribute('data-latest-bg') || '';
                if (isCdnUrl(bg)) {
                    base = bg.replace(/=[^/]*$/, '');
                }
            }
        }

        if (!base || !isCdnUrl(base)) continue;

        const cdnId = base.split('/').pop();
        if (!cdnId || cdnId.length < 16 || seen.has(cdnId)) continue;
        seen.add(cdnId);

        // Detect videos via the anchor's own aria-label ("Video - 0:20 - ...")
        // which is more reliable than inspecting the parent tile element.
        const ariaLabel = (a.getAttribute('aria-label') || '').toLowerCase();
        const isVideo = ariaLabel.startsWith('video') ||
                        !!(a.querySelector('[data-video-url]'));

        results.push({ cdnId, base, isVideo, href: a.getAttribute('href') });
    }
    return results;
}
"""


async def _extract_items(page: Page) -> list[dict]:
    return await page.evaluate(_EXTRACT_JS)


async def _scroll_and_collect(page: Page, *, stable_rounds: int = 5) -> dict[str, dict]:
    """
    Scroll to the bottom of the current page, collecting all visible media.
    Stops when no new items appear for stable_rounds consecutive scrolls.
    Returns a dict keyed by cdnId.
    """
    seen: dict[str, dict] = {}
    no_new = 0

    await page.wait_for_load_state("domcontentloaded")

    # Wait for the Google Photos JS app to render at least one photo tile
    # before we start scrolling; domcontentloaded fires before React renders.
    try:
        await page.wait_for_selector(
            'a[href*="/photo/"]', state="attached", timeout=20_000
        )
    except Exception:
        log.debug("No photo links appeared within 20 s — page may be empty.")

    if log.isEnabledFor(logging.DEBUG):
        url = page.url
        title = await page.title()
        counts = await page.evaluate("""() => ({
            photoLinks:  document.querySelectorAll('a[href*=\\"/photo/\\"]').length,
            albumLinks:  document.querySelectorAll('a[href*=\\"/albums/\\"]').length,
            allAnchors:  document.querySelectorAll('a[href]').length,
            allImgs:     document.querySelectorAll('img').length,
            cdnImgs:     document.querySelectorAll('img[src*=\\"googleusercontent\\"]').length,
            dataLatestBg:document.querySelectorAll('[data-latest-bg]').length,
            sampleHrefs: [...document.querySelectorAll('a[href]')]
                            .slice(0, 10).map(a => a.href),
            sampleBgValues: [...document.querySelectorAll('[data-latest-bg]')]
                            .slice(0, 3).map(el => el.getAttribute('data-latest-bg')),
            firstPhotoTileHTML: (() => {
                const a = document.querySelector('a[href*=\\"/photo/\\"]');
                return a ? a.parentElement.outerHTML.slice(0, 1000) : null;
            })(),
        })""")
        log.debug("URL: %s", url)
        log.debug("Title: %s", title)
        log.debug("DOM counts: %s", counts)

        extract_diag = await page.evaluate("""() => {
            const isCdnUrl = u => u.includes('googleusercontent.com') ||
                                   u.includes('usercontent.google.com');
            const out = [];
            let n = 0;
            for (const a of document.querySelectorAll('a[href*="/photo/"]')) {
                if (n++ >= 3) break;
                const img = a.querySelector('img');
                const imgSrc = img ? (img.src || img.getAttribute('data-src') || '') : '';
                const tile = a.closest('[data-latest-bg]');
                const bg   = tile ? (tile.getAttribute('data-latest-bg') || '') : '';
                const inner = a.querySelector('[style*="background-image"]');
                const innerStyle = inner ? (inner.getAttribute('style') || '') : '';
                const styleMatch = innerStyle.match(/url\\(["']?(https?:\\/\\/[^"')]+)/);
                out.push({
                    href:         a.getAttribute('href'),
                    hasTile:      !!tile,
                    bgIsCdn:      isCdnUrl(bg),
                    hasInner:     !!inner,
                    innerStyleSlice: innerStyle.slice(0, 120),
                    styleMatchUrl:   styleMatch ? styleMatch[1].slice(0, 80) : null,
                    imgSrcIsCdn:  isCdnUrl(imgSrc),
                });
            }
            return out;
        }""")
        log.debug("Extraction diagnostic: %s", extract_diag)

        screenshot_path = DATA_DIR / "debug_screenshot.png"
        await page.screenshot(path=str(screenshot_path), full_page=False)
        log.debug("Screenshot saved to %s", screenshot_path)

    while no_new < stable_rounds:
        items = await _extract_items(page)
        added = sum(1 for it in items if it["cdnId"] not in seen)
        for it in items:
            seen.setdefault(it["cdnId"], it)

        no_new = 0 if added else no_new + 1

        # Scroll one viewport at a time so new tiles enter the viewport and
        # get their background-image style set (Google Photos only renders
        # the CDN URL for tiles that are actually visible on screen).
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(2500)

    return seen


async def _shared_album_urls(page: Page) -> list[str]:
    """Navigate to the sharing page and return URLs for all visible shared albums."""
    await page.goto(SHARING_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2000)

    return await page.evaluate("""
        () => [...new Set(
            [...document.querySelectorAll('a[href*="/albums/"]')]
            .map(a => a.href)
            .filter(h => h.includes('photos.google.com'))
        )]
    """)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _build_session(cookies: list[dict]) -> requests.Session:
    """Build a requests.Session loaded with the browser's Google cookies."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        # Referer is required — without it lh3.googleusercontent.com may return 403
        "Referer": "https://photos.google.com/",
    })
    for c in cookies:
        if "google" in c.get("domain", ""):
            s.cookies.set(c["name"], c["value"], domain=c["domain"].lstrip("."))
    return s


def _ext_from_mime(content_type: str) -> str:
    return MIME_TO_EXT.get(content_type.split(";")[0].strip().lower(), "")


def _filename_from_cd(cd: str) -> str | None:
    """Extract filename from a Content-Disposition header."""
    m = re.search(r"filename\*=(?:UTF-8'')?([^\s;]+)", cd, re.I)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1).strip('"'))
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    return m.group(1).strip() if m else None


def _dest_path(filename: str) -> Path:
    """Place file under YYYY/MM inferred from filename, or 'unsorted'."""
    m = re.search(r"(\d{4})(\d{2})\d{2}", filename)
    subfolder = f"{m.group(1)}/{m.group(2)}" if m else "unsorted"
    return PHOTOS_DIR / subfolder / filename


def download_item(
    session: requests.Session,
    item: dict,
    state: dict,
    *,
    dry_run: bool = False,
) -> bool:
    """Download a single media item. Returns True if newly downloaded."""
    cdn_id = item["cdnId"]

    if cdn_id in state["downloaded"]:
        existing = Path(state["downloaded"][cdn_id])
        if existing.exists():
            return False
        log.info("Re-downloading (file missing): %s", cdn_id[:24])

    if dry_run:
        log.info("[DRY RUN] %s", cdn_id[:32])
        return False

    # Try the most likely suffix first; fall back to the other
    suffixes = ("=dv", "=d") if item.get("isVideo") else ("=d", "=dv")

    for attempt in range(4):
        for suffix in suffixes:
            url = item["base"] + suffix
            try:
                with session.get(url, stream=True, timeout=300) as resp:
                    if resp.status_code == 404:
                        continue  # wrong suffix — try the other one
                    resp.raise_for_status()

                    ct = resp.headers.get("Content-Type", "")
                    ext = _ext_from_mime(ct)
                    fname = (
                        _filename_from_cd(resp.headers.get("Content-Disposition", ""))
                        or (cdn_id[:32] + ext)
                    )

                    dest = _dest_path(fname)
                    if dest.exists() and cdn_id not in state["downloaded"]:
                        dest = dest.with_stem(dest.stem + "_" + cdn_id[:8])
                    dest.parent.mkdir(parents=True, exist_ok=True)

                    tmp = dest.with_suffix(dest.suffix + ".part")
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_content(65536):
                            fh.write(chunk)
                    tmp.rename(dest)

                    state["downloaded"][cdn_id] = str(dest)
                    log.info("Downloaded: %s", dest.name)
                    return True

            except requests.RequestException as exc:
                log.warning(
                    "Download error (attempt %d, suffix %s): %s",
                    attempt + 1, suffix, exc,
                )
                break  # retry outer loop with backoff

        if attempt < 3:
            time.sleep(2 ** attempt)

    log.error("Failed to download %s after 4 attempts", cdn_id[:24])
    return False


# ---------------------------------------------------------------------------
# State
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
# Backup orchestration
# ---------------------------------------------------------------------------

async def do_backup(args: argparse.Namespace) -> None:
    state = _load_state()
    counts = {"downloaded": 0, "skipped": 0, "errors": 0}

    async with async_playwright() as pw:
        # Prefer the portable session.json (cross-platform: works when auth was
        # run on macOS and backup runs on Linux, since macOS Keychain-encrypted
        # cookies in browser-data/ cannot be decrypted on Linux).
        _browser = None
        if SESSION_FILE.exists():
            log.debug("Loading portable session from %s", SESSION_FILE)
            _browser = await pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
            ctx = await _browser.new_context(
                storage_state=str(SESSION_FILE),
                viewport={"width": 1280, "height": 900},
                accept_downloads=True,
            )
        else:
            log.debug("No session.json found; falling back to browser-data profile.")
            ctx = await _open_context(pw, headless=True)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        if not await _is_signed_in(page):
            log.error(
                "Not signed in to Google Photos.\n"
                "Run '--auth-only' on a machine with a display (or locally),\n"
                "then copy %s and %s to this server.",
                BROWSER_DATA_DIR, SESSION_FILE,
            )
            await ctx.close()
            if _browser:
                await _browser.close()
            sys.exit(1)

        cookies = await ctx.cookies([
            "https://photos.google.com",
            "https://www.google.com",
            "https://lh3.googleusercontent.com",
            "https://photos.fife.usercontent.google.com",
        ])
        session = _build_session(cookies)

        def process(item: dict) -> None:
            try:
                ok = download_item(session, item, state, dry_run=args.dry_run)
                counts["downloaded" if ok else "skipped"] += 1
                n = counts["downloaded"]
                if n and n % 25 == 0:
                    _save_state(state)
                    log.info(
                        "Progress — downloaded: %d  skipped: %d  errors: %d",
                        n, counts["skipped"], counts["errors"],
                    )
            except Exception as exc:
                log.error("Error on %s: %s", item.get("cdnId", "?")[:24], exc)
                counts["errors"] += 1

        if not args.shared_only:
            log.info("Scanning library…")
            await page.goto(GPHOTOS_URL, wait_until="domcontentloaded")
            items = await _scroll_and_collect(page)
            log.info("Found %d items in library.", len(items))
            for item in items.values():
                process(item)

        if args.include_shared or args.shared_only:
            log.info("Scanning shared albums…")
            album_urls = await _shared_album_urls(page)
            log.info("Found %d shared albums.", len(album_urls))
            for url in album_urls:
                log.info("  Album: %s", url)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    items = await _scroll_and_collect(page)
                    log.info("  Found %d items.", len(items))
                    for item in items.values():
                        process(item)
                except Exception as exc:
                    log.error("Error scanning album %s: %s", url, exc)

        await ctx.close()
        if _browser:
            await _browser.close()

    _save_state(state)
    log.info(
        "Done — downloaded: %d  skipped: %d  errors: %d",
        counts["downloaded"], counts["skipped"], counts["errors"],
    )
    if counts["errors"]:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Back up Google Photos via browser automation — no Photos Library API required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Auth (once — requires a display):
  docker compose run --rm gphotos-auth            # on a local machine with X11

  Or auth locally and copy the session to your server:
  rsync -av ./config/browser-data/ user@server:/path/to/config/browser-data/

Backup (headless — runs anywhere after auth):
  docker compose run --rm gphotos-backup
  docker compose run --rm gphotos-backup --include-shared
  docker compose run --rm gphotos-backup --shared-only --dry-run
""",
    )
    parser.add_argument(
        "--auth-only", action="store_true",
        help="Open a headed browser to sign in and save the session, then exit.",
    )
    parser.add_argument("--include-shared", action="store_true",
                        help="Also download albums shared with you.")
    parser.add_argument("--shared-only", action="store_true",
                        help="Only download albums shared with you.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be downloaded without downloading.")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose debug logging.")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    if args.include_shared and args.shared_only:
        parser.error("--include-shared and --shared-only are mutually exclusive.")

    if args.auth_only:
        asyncio.run(do_auth())
    else:
        asyncio.run(do_backup(args))


if __name__ == "__main__":
    main()
