FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5001 \
    FLASK_HOST=0.0.0.0 \
    DATABASE_PATH=/data/tms.db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system fleetflow \
    && useradd --system --gid fleetflow --home-dir /app fleetflow \
    && mkdir -p /data /app/uploads \
    && chown -R fleetflow:fleetflow /data /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=fleetflow:fleetflow . .

USER fleetflow

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import socket; s=socket.create_connection(('127.0.0.1', 5001), timeout=3); s.close()" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "2", "--timeout", "120", "app:app"]
