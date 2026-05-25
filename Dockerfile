FROM python:3.14-alpine

WORKDIR /app

# Install dependencies first so this layer is cached across code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ .

# Run as a non-root user
RUN adduser -D -H -u 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "main.py"]
