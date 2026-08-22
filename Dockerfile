# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base

# Unbuffered so logs reach the collector immediately; no .pyc to keep the layer
# small and the filesystem read-only friendly.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build deps for argon2-cffi and psycopg, removed in the same layer so they do
# not ship in the image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && apt-get purge -y build-essential && apt-get autoremove -y

COPY . .

# Collected at build time so the container starts without needing write access
# to its own filesystem.
RUN SECRET_KEY=build-only DEBUG=True python manage.py collectstatic --noinput

# Never run as root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD curl -fsS http://localhost:8000/healthz || exit 1

# Two workers per core is the usual starting point; threads help because most
# of this is database-bound rather than CPU-bound.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", "--threads", "4", \
     "--timeout", "60", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-"]
