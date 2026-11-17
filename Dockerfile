# syntax=docker/dockerfile:1
FROM node:22-slim AS frontend-builder
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN mkdir -p /src/digital_employee/web/static && npx tsc -b && npx vite build --outDir /dist-static

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY configs ./configs
COPY --from=frontend-builder /dist-static ./src/digital_employee/web/static

RUN uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python -e .

ENV PATH="/app/.venv/bin:${PATH}" \
    DE_DATA_DIR="/data" \
    DE_WEB_HOST="0.0.0.0" \
    DE_WEB_PORT="8020" \
    PYTHONDONTWRITEBYTECODE="1"

VOLUME ["/data"]
EXPOSE 8020

HEALTHCHECK --interval=20s --timeout=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8020/api/health || exit 1

CMD ["de", "serve", "--host", "0.0.0.0", "--port", "8020"]
