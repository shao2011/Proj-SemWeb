FROM python:3.12-slim

# MEDIA_ROOT's code default is the relative "media", which under /app would
# be root-owned. Pinned here to the volume; compose's data/media is the same
# place once uvicorn starts from /app.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEDIA_ROOT=/app/data/media

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY backend ./backend

# Installs exactly what uv.lock pins (hash-checked), then the project itself
# without letting pip resolve anything. `--locked` fails the build when the
# lock does not match pyproject.toml instead of quietly re-resolving.
# nlpo3 has no prebuilt wheel for linux/aarch64, so its Rust extension builds
# from source; the toolchain and uv are removed again to keep the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends cargo rustc \
 && pip install --no-cache-dir uv==0.11.28 \
 && uv export --locked --no-dev --no-emit-project -o /tmp/requirements.txt \
 && pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
 && pip install --no-cache-dir --no-deps . \
 && pip uninstall -y uv \
 && rm /tmp/requirements.txt \
 && apt-get purge -y cargo rustc \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# The app writes only media (MEDIA_ROOT under /app/data, the compose volume,
# when R2 is not configured) and temp files. /app/run holds the copied
# Firebase credential when the mounted one is root-only.
RUN groupadd --system --gid 10001 app \
 && useradd --system --create-home --uid 10001 --gid app app \
 && mkdir -p /app/data /app/run \
 && chown app:app /app/data /app/run \
 && chmod 0700 /app/run

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

# The entrypoint starts as root and drops to uid 10001 before running this.
ENTRYPOINT ["sh", "/usr/local/bin/docker-entrypoint.sh"]

# Alembic owns the schema. It runs from backend/ because alembic.ini lives there
# and resolves script_location relative to itself.
CMD ["sh", "-c", "cd backend && alembic upgrade head && cd /app && exec uvicorn rag_ingestion.api.app:app --host 0.0.0.0 --port 8000"]
