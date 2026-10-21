FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY backend ./backend

# nlpo3 has no prebuilt wheel for linux/aarch64, so its Rust extension builds
# from source; the toolchain is purged again to keep the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends cargo rustc \
 && pip install --no-cache-dir . \
 && apt-get purge -y cargo rustc \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

EXPOSE 8000

# Alembic owns the schema. It runs from backend/ because alembic.ini lives there
# and resolves script_location relative to itself.
CMD ["sh", "-c", "cd backend && alembic upgrade head && cd / && exec uvicorn rag_ingestion.api.app:app --host 0.0.0.0 --port 8000"]
