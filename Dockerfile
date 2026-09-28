# syntax=docker/dockerfile:1
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

RUN useradd --system --uid 10001 --home-dir /srv --shell /usr/sbin/nologin app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY data/wage_codes.csv ./data/wage_codes.csv

# Writable state lives outside the code tree: mount /srv/var/db as a volume so users survive restarts.
# Jobs are transient by design; a tmpfs or ephemeral volume is fine for /srv/var/jobs.
RUN mkdir -p /srv/var/db /srv/var/jobs && chown -R app:app /srv/var
ENV WTC_USER_DB_PATH=/srv/var/db/users.db \
    WTC_JOB_DIR=/srv/var/jobs \
    WTC_LOG_FORMAT=json

USER app
EXPOSE 8000
VOLUME ["/srv/var/db"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]

# One worker: login throttling is per process. Scale with replicas behind a proxy that enforces rate limits.
# Behind a reverse proxy, set FORWARDED_ALLOW_IPS to the proxy's address so client IPs are real.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "20"]
