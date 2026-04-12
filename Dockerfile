# Playwright's official image ships Python 3.12, Chromium, and all system
# dependencies pre-installed — no separate `playwright install` step needed.
FROM mcr.microsoft.com/playwright/python:v1.51.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backup.py .

# /config  — browser-data/ (persistent Chromium profile with saved session)
# /data    — downloaded photos + backup_state.json
VOLUME ["/config", "/data"]

ENTRYPOINT ["python", "backup.py"]
