FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backup.py .

# /config  — OAuth credentials.json and saved token.json
# /data    — downloaded photos + backup_state.json
VOLUME ["/config", "/data"]

# Used only during first-time OAuth flow
EXPOSE 8080

ENTRYPOINT ["python", "backup.py"]
