# gphotos-backup

A Docker container that downloads full-quality photos and videos from Google Photos — both your own library and albums shared with you.

Files are organised on disk as:

```
data/
  photos/
    2024/
      01/
        IMG_1234.jpg
        VID_5678.mp4
      02/
        ...
  backup_state.json   ← tracks what has already been downloaded
```

---

## 1. Create Google Cloud OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project (or select an existing one).
2. Enable the **Photos Library API**: *APIs & Services → Library → search "Photos Library API" → Enable*.
3. Configure the OAuth consent screen: *APIs & Services → OAuth consent screen*.
   - User type: **External** (or Internal if you use Workspace).
   - Add the scope `https://www.googleapis.com/auth/photoslibrary.readonly`.
   - Add your Google account as a test user.
4. Create credentials: *APIs & Services → Credentials → Create Credentials → OAuth client ID*.
   - Application type: **Desktop app**.
5. Download the JSON file and save it as **`config/credentials.json`** next to this repository.

---

## 2. Authenticate (first run only)

The OAuth redirect goes to `http://localhost:8080`, so port 8080 must be reachable from your browser to the container.

```bash
mkdir -p config data

# Build the image
docker compose build

# Trigger the OAuth flow (port 8080 forwarded)
docker compose run --rm gphotos-backup --auth-only
```

The script will print a Google authorization URL. Open it in your browser, grant access, and the browser will redirect to `localhost:8080` — the container captures the code and saves a token to `config/token.json`.

Once `config/token.json` exists you no longer need port 8080 for backups.

---

## 3. Run a backup

```bash
# Your own library only
docker compose run --rm gphotos-backup

# Your library + all albums shared with you
docker compose run --rm gphotos-backup --include-shared

# Only shared albums (useful if the library belongs to someone else)
docker compose run --rm gphotos-backup --shared-only

# Preview what would be downloaded without downloading anything
docker compose run --rm gphotos-backup --include-shared --dry-run
```

### Running without docker compose

```bash
docker run --rm \
  -v "$(pwd)/config:/config" \
  -v "$(pwd)/data:/data" \
  gphotos-backup \
  --include-shared
```

---

## 4. Scheduling

Add a cron entry on the host to run the backup nightly:

```cron
0 2 * * * cd /path/to/gphotos-backup && docker compose run --rm gphotos-backup --include-shared >> /var/log/gphotos-backup.log 2>&1
```

---

## Options

| Flag | Description |
|---|---|
| `--auth-only` | Authenticate and save token, then exit. Requires `-p 8080:8080`. |
| `--include-shared` | Also download from albums shared with you. |
| `--shared-only` | Only download from albums shared with you. |
| `--dry-run` | List what would be downloaded without downloading. |
| `--debug` | Verbose logging. |

Environment variables `CONFIG_DIR` and `DATA_DIR` override the default `/config` and `/data` paths.

---

## How it works

- **Photos** are downloaded at original quality using `baseUrl + "=d"`.
- **Videos** are downloaded as the original video file using `baseUrl + "=dv"` (without this suffix you get only a thumbnail).
- The `baseUrl` returned by the API expires after ~1 hour, so the script re-fetches each item's URL immediately before downloading.
- `backup_state.json` maps Google Photos item IDs to local paths. A file is skipped on subsequent runs if its ID is already recorded **and** the local file still exists.
- Downloads use a `.part` temporary file and rename atomically on completion, so interrupted runs leave no corrupt files.
