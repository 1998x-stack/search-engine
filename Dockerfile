# Phase 1: API scaffold and opt-in crawl CLI, no search index/search API implementation yet.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SEARCH_SCHEMA_PATH=/app/schema/sqlite.sql
WORKDIR /app
RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --home-dir /app app && \
    mkdir -p /data && chown app:app /data
COPY pyproject.toml README.md /app/
COPY src /app/src
COPY schema /app/schema
COPY migrations /app/migrations
RUN python -m pip install --no-cache-dir .
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health/live',timeout=2).read()" || exit 1
CMD ["uvicorn", "search_engine.api:app", "--host", "0.0.0.0", "--port", "8000"]
