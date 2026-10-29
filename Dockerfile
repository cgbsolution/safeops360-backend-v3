# syntax=docker/dockerfile:1.7
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=10

# No apt-get step: every dependency ships a prebuilt manylinux wheel
# (psycopg2-binary, asyncpg, bcrypt, cryptography), so no compiler or libpq
# is needed. This also keeps builds working when deb.debian.org is unreachable.

WORKDIR /app

# Install deps first for layer caching
COPY pyproject.toml /app/
RUN pip install --upgrade pip && pip install --prefer-binary -e .

# App code
COPY app /app/app
COPY alembic /app/alembic
COPY alembic.ini /app/

# Non-root runtime user
RUN useradd --create-home --uid 1001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Healthcheck — Render / Fly / Railway will use this
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

# Single-process default; scale with the platform's replicas, not gunicorn workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
