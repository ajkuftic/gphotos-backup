FROM python:3.12-slim

WORKDIR /app

# yt-dlp needs ffmpeg for muxing (used only when merging formats; not for URL extraction)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["uvicorn", "app.main:api", "--host", "0.0.0.0", "--port", "8080"]
