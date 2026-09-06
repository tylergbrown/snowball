FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt README.md ./
COPY snowball ./snowball

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps .

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin snowball \
    && mkdir -p /app/data \
    && chown -R snowball:snowball /app

USER snowball

EXPOSE 8080

ENV PYTHONUNBUFFERED=1 \
    MODE=paper \
    LIVE_ENABLED=false \
    TRADING_ENABLED=true \
    HALT_FILE=/app/data/HALT \
    SQLITE_PATH=/app/data/snowball.db \
    HEARTBEAT_PATH=/app/data/heartbeat \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

CMD ["python", "-m", "snowball"]
