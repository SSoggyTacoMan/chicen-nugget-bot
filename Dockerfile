FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir --only-binary :all: --require-hashes -r requirements.lock \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

COPY main.py slack-app-manifest.json ./
COPY bot ./bot
COPY prompts ./prompts
COPY scripts/backfill_memory.py ./scripts/backfill_memory.py

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/logs \
    && chown -R appuser:appuser /app \
    && chmod 700 /app/data /app/logs

USER appuser

CMD ["python", "main.py"]
