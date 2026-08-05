# ── DeepSeek Chat API Proxy — minimal image ────────────────────────────────
# Usage:
#   docker compose up -d --build                 # build locally (dev)
#   docker compose pull && docker compose up -d  # pull from GHCR mirror (prod)
#
# Network note — if this host runs a system proxy (e.g. Clash on
# 127.0.0.1:7890) and `pip install` fails during the build:
#
#   - Symptom "ProxyError: Cannot connect to proxy": BuildKit auto-injects
#     the host's proxy env into the build, but 127.0.0.1 inside the build
#     container is the container itself, NOT your host. docker-compose.yml
#     clears the proxies by default; to route pip through your host proxy
#     pass the HOST GATEWAY address (not 127.0.0.1):
#        docker compose build --build-arg http_proxy=http://172.17.0.1:7890 \
#                              --build-arg https_proxy=http://172.17.0.1:7890
#   - Or skip the proxy and use a PyPI mirror (simplest for CN networks):
#        docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#   - Or set them via .env: BUILD_HTTP_PROXY / BUILD_HTTPS_PROXY /
#     BUILD_PIP_INDEX_URL (compose forwards them as build args).
#
# CI (GitHub Actions) builds the multi-arch image and pushes it to GHCR;
# see .github/workflows/docker-image.yml.

FROM python:3.12-slim

# Optional proxy / mirror build args (only affect `pip install`).
# Empty by default so a plain `docker compose up -d --build` works when
# the network can reach PyPI directly. BuildKit auto-injects the host's
# HTTP_PROXY/HTTPS_PROXY as these args — compose explicitly overrides them
# with empty values (or BUILD_* from .env) to avoid the 127.0.0.1 trap.
ARG http_proxy=
ARG https_proxy=
ARG all_proxy=
ARG no_proxy=
ARG PIP_INDEX_URL=
ENV http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    all_proxy=${all_proxy} \
    no_proxy=${no_proxy:-localhost,127.0.0.1,::1} \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1) Python deps first (layer-cache friendly: requirements change rarely).
COPY requirements.txt .
RUN pip install -r requirements.txt

# 2) Application code + PoW WASM engine. `.dockerignore` keeps secrets
#    (.env, data/) and build artefacts out of the image.
COPY . .

# 3) Single-process service; run as non-root.
RUN useradd -r -u 1001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Config is injected at runtime via environment (env_file in compose);
# the container never contains a baked-in .env.
CMD ["python", "server.py"]
