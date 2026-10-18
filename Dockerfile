# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5001 \
    FLASK_HOST=0.0.0.0 \
    DATABASE_PATH=/data/tms.db

WORKDIR /app

# Install only the application's declared Python dependencies.
COPY expense-tracker/requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

# Copy the Flask application, templates, static assets, and modules.
COPY expense-tracker/ ./

# Named volumes can inherit these permissions on first creation.
RUN mkdir -p /data /app/instance/driver_documents \
    && chown -R 10001:10001 /app /data

USER 10001:10001

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/', timeout=3)" || exit 1

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-5001} --workers 2 --timeout 60 app:app"]
