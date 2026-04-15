# gphotos-backup

[![Build and push Docker image](https://github.com/ajkuftic/gphotos-backup/actions/workflows/docker.yml/badge.svg)](https://github.com/ajkuftic/gphotos-backup/actions/workflows/docker.yml)

A Docker container that downloads full-quality photos and videos from Google Photos — both your own library and albums shared with you.

**No Google Cloud project or API credentials required.** Authentication uses a browser session (same as logging in at photos.google.com), which sidesteps the Google Photos Library API access restrictions.

Files are organised as:

```
data/
  photos/
    2024/
      01/
        IMG_20240115_142030.jpg
        VID_20240115_143200.mp4
      02/
        ...
    unsorted/      ← items whose date can't be inferred from filename
  backup_state.json
```

---

## How it works

- Playwright (headless Chromium) logs in to `photos.google.com` using a saved browser session
- Scrolls through your library and shared albums, collecting CDN URLs (`lh3.googleusercontent.com/...`)
- Downloads each item at full quality: `base_url + "=d"` for photos, `"=dv"` for videos
- Tracks downloaded items by CDN ID in `backup_state.json` so re-runs skip already-downloaded files

---

## 1. Authenticate (once)

The auth step opens a headed browser window so you can sign in to Google. **This must be run on a machine with a display** (your laptop or desktop, not a headless server).

```bash
mkdir -p config data

# Pull the image
docker compose pull

# Open the browser, sign in to Google Photos, close when redirected to the library
docker compose run --rm gphotos-auth
```

> **macOS / Windows:** Replace the `gphotos-auth` service command with:
> ```bash
> docker run --rm -v "$(pwd)/config:/config" \
>   -e DISPLAY=host.docker.internal:0 \
>   ajkuftic/gphotos-backup:latest --auth-only
> ```
> You'll need XQuartz (macOS) or VcXsrv (Windows) running.

The session is saved to `config/browser-data/` and `config/session.json`.

> `session.json` is a portable, cross-platform export of your Google session cookies. It is what the backup container actually uses, so it is the critical file to copy.

### Authenticating for a remote server (NAS)

Run auth on your local machine, then copy the session:

```bash
# On your local machine
docker compose run --rm gphotos-auth

# Copy to the remote server (only session.json is strictly required)
rsync -av ./config/session.json user@your-nas:/path/to/gphotos-backup/config/session.json
```

After that, all backup runs on the server are fully headless — no display needed.

---

## 2. Run a backup

```bash
# Your own library only
docker compose run --rm gphotos-backup

# Your library + all albums shared with you
docker compose run --rm gphotos-backup --include-shared

# Only shared albums
docker compose run --rm gphotos-backup --shared-only

# Preview what would be downloaded without downloading
docker compose run --rm gphotos-backup --include-shared --dry-run
```

---

## 3. Scheduling

```cron
0 2 * * * cd /path/to/gphotos-backup && docker compose run --rm gphotos-backup --include-shared >> /var/log/gphotos-backup.log 2>&1
```

---

## Options

| Flag | Description |
|---|---|
| `--auth-only` | Open headed browser to sign in and save session, then exit |
| `--include-shared` | Also download albums shared with you |
| `--shared-only` | Only download albums shared with you |
| `--dry-run` | List what would be downloaded without downloading |
| `--debug` | Verbose logging |

Environment variables `CONFIG_DIR` and `DATA_DIR` override the default `/config` and `/data` paths.

---

## Session expiry

Google sessions typically last weeks to months. When the session expires, the backup will log "Not signed in" and exit. Re-run `gphotos-auth` to refresh it.

---

## Docker Hub

```bash
docker pull ajkuftic/gphotos-backup:latest
```

Images are built automatically on every push to `main` and on version tags (`v1.2.3`), for both `linux/amd64` and `linux/arm64`.

### Publishing setup (maintainer)

Two GitHub Actions secrets are required:

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub username |
| `DOCKERHUB_TOKEN` | Docker Hub access token (read/write) |

To cut a release:
```bash
git tag v1.0.0
git push origin v1.0.0
```
