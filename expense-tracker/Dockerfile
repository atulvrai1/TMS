FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5001 \
    FLASK_HOST=0.0.0.0 \
    DATABASE_PATH=/data/tms.db

WORKDIR /app

# Install system dependencies and create a non-root application user
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system fleetflow \
    && useradd --system --gid fleetflow --home-dir /app fleetflow \
    && mkdir -p /data /app/instance/driver_documents \
    && chown -R fleetflow:fleetflow /app /data

# Copy Python dependencies first for better Docker layer caching
COPY requirements.txt ./

# Install Python dependencies with longer timeout and additional retries
RUN python -m pip install \
    --no-cache-dir \
    --default-timeout=120 \
    --retries=10 \
    -r requirements.txt

# Copy application source code
COPY --chown=fleetflow:fleetflow . .

# Ensure runtime directories exist and have the correct permissions
RUN mkdir -p /data /app/instance/driver_documents \
    && chown -R fleetflow:fleetflow /app /data

# Run the application without root privileges
USER fleetflow

EXPOSE 5001

# Container health check
HEALTHCHECK --interval=30s \
    --timeout=5s \
    --start-period=20s \
    --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '5001') + '/', timeout=4)" || exit 1

# Start the Flask application using Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "2", "--timeout", "120", "app:app"]
