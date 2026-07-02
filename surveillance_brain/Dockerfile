# ──────────────────────────────────────────────────────────────────────────
# Dockerfile for the Surveillance DB System FastAPI app.
# Multi-stage to keep the final image small (~250 MB).
# ──────────────────────────────────────────────────────────────────────────

# Stage 1: build dependencies in a throwaway layer
FROM python:3.12-slim AS builder

WORKDIR /build

# System deps required by psycopg-binary and asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ──────────────────────────────────────────────────────────────────────────
# Stage 2: runtime
# ──────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Runtime system deps — only what's needed to run asyncpg + psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from the builder stage
COPY --from=builder /install /usr/local

# App working directory
WORKDIR /app

# Copy the project (respects .dockerignore)
COPY . .

# Expose the FastAPI port
EXPOSE 8000

# Healthcheck — hits /health every 30s; container is unhealthy after 3 fails
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Run the app with uvicorn — single worker by default; scale via
# `docker compose up --scale app=N` if needed (Redis + asyncpg both
# support concurrent workers cleanly).
CMD ["uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
