# ── DeepSeek Chat API Proxy — image with WebUI baked in ────────────────────
# Usage:
#   docker compose up -d --build                 # build locally (dev)
#   docker compose pull && docker compose up -d  # pull from GHCR mirror (prod)
#
# The React WebUI (webui-new/) is built in a Node stage and baked into the
# final image — no separate front-end build step needed at deploy time.
#
# Network note — if this host runs a system proxy (e.g. Clash on
# 127.0.0.1:7890), the build itself disables proxy env vars in both stages:
# BuildKit injects the Docker daemon's HTTP(S)_PROXY into every build
# container, but 127.0.0.1 inside it is the container itself (not your host),
# which breaks `pip install` / `npm ci` with "Cannot connect to proxy".
# Package retrieval is done directly from the configured indexes/mirrors:
#   - PyPI mirror (CN):  BUILD_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#   - npm mirror (CN):   BUILD_NPM_REGISTRY=https://registry.npmmirror.com
# (set either in .env; compose forwards them as build args).
#
# CI (GitHub Actions) builds the multi-arch image and pushes it to GHCR;
# see .github/workflows/docker-image.yml.

# ── Stage 1: build the React WebUI (webui-new/) ────────────────────────────
FROM node:20-slim AS webui-builder
WORKDIR /app/webui-new

# BuildKit injects the Docker daemon's HTTP(S)_PROXY into every build
# container; 127.0.0.1 inside it is the container itself (not the host),
# which breaks npm. Unset them in the RUN shell for npm steps.
ARG NPM_REGISTRY=

RUN for var in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy all_proxy ALL_PROXY; do unset "$var"; done && \
    if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi

# Install deps first (layer-cache friendly), then build -> ./dist
COPY webui-new/package.json webui-new/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY webui-new/ ./
RUN npm run build

# ── Stage 2: Python runtime ────────────────────────────────────────────────
FROM python:3.12-slim

# Optional PyPI mirror build arg (compose forwards BUILD_PIP_INDEX_URL).
ARG PIP_INDEX_URL=
# Force proxy OFF in pip's RUN step. BuildKit injects the Docker daemon's
# HTTP(S)_PROXY into every build container; a subsequent ENV https_proxy=""
# does NOT override it because BuildKit re-applies the inject after ENV.
# We unset them in the RUN shell for the pip step instead.
ENV PIP_INDEX_URL=${PIP_INDEX_URL:-} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/app/.tiktoken-cache

WORKDIR /app

# 1) Python deps first (layer-cache friendly: requirements change rarely).
COPY requirements.txt .
RUN for var in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy all_proxy ALL_PROXY; do unset "$var"; done && \
    pip install -r requirements.txt

# 2) Application code + PoW WASM engine. `.dockerignore` keeps secrets
#    (.env, data/) and build artefacts out of the image.
COPY . .

# 3) Bake in the WebUI built in stage 1.
COPY --from=webui-builder /app/webui-new/dist ./webui-new/dist

# 4) Single-process service; run as non-root.
RUN useradd -r -u 1001 appuser \
    && mkdir -p /app/data /app/.tiktoken-cache \
    && chown -R appuser:appuser /app

# Warm the tiktoken cl100k_base vocab cache into the image so `usage` token
# counts stay exact when the runtime cannot reach openaipublic.blob.core
# windows.net (CN networks). Non-fatal: offline builds skip, CI warms it.
USER appuser
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" || true

EXPOSE 28080

# Config is injected at runtime via environment (env_file in compose);
# the container never contains a baked-in .env.
CMD ["python", "server.py"]
