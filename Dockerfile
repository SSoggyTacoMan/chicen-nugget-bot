FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

COPY . .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/logs \
    && chown -R appuser:appuser /app \
    && chmod 700 /app/data /app/logs

USER appuser

CMD ["python", "main.py"]
