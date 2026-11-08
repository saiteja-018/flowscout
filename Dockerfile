FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    useradd --create-home --uid 10001 flowscout && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Mock-mode console runs use a real local Chromium, so the image ships the
# browser and its system libraries. Live mode still uses the Celesto guest.
RUN pip install --retries 5 -r requirements.txt && \
    python -m playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/*

COPY --chown=flowscout:flowscout . .

RUN install -d -o flowscout -g flowscout logs reports/evidence reports/console reports/console/runs

USER flowscout

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=20s \
    CMD curl -fsS "http://localhost:${PORT:-8080}/health" || exit 1

# Render supplies PORT. Mock console runs use the bundled Chromium; live
# Celesto runs still require credentials, explicit authorization, and a
# configured sandbox size.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
