FROM python:3.14-alpine

WORKDIR /app

# FFmpeg provides the single-frame fallback for moving media. libstdc++ is
# required by the prebuilt rlottie wheel used for animated TGS stickers.
RUN apk add --no-cache ffmpeg libstdc++

# Install dependencies first so this layer is cached across code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY naruto/ ./naruto/

# Run as a non-root user. /data holds the SQLite database (mount a volume).
RUN adduser -D -H -u 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

# Inside the container the web admin listens on all interfaces so Docker can
# publish it; docker-compose.yml publishes it on the host's 127.0.0.1 only.
ENV DATABASE_PATH=/data/naruto.db \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8765 \
    PYTHONUNBUFFERED=1
EXPOSE 8765
VOLUME ["/data"]

CMD ["python", "-m", "naruto"]
