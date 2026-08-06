# syntax=docker/dockerfile:1.7
#
# f126-race-engineer — multi-stage image, target platform linux/amd64 (the k3s node it runs on).
# Build from the REPO ROOT so the context contains both frontend/ and src/:
#   docker buildx build --platform linux/amd64 -f deploy/Dockerfile -t <tag> .
#
# Layer ordering rule throughout: lockfiles first, install, THEN source. Editing a .py or
# .svelte file must never invalidate a dependency layer.
#
# No HEALTHCHECK on purpose — Kubernetes liveness/readiness probes own that (deployment.yaml).

# ---------------------------------------------------------------------------
# Stage 1 — frontend build (Svelte 5 + Vite 8) -> /build/dist
# ---------------------------------------------------------------------------
# Pinned to $BUILDPLATFORM: the output is platform-independent static JS/CSS, so this stage
# runs natively on the builder (arm64 Mac, amd64 CI) instead of under slow QEMU emulation.
FROM --platform=$BUILDPLATFORM node:24-slim AS frontend

WORKDIR /build

# Dependency layer: invalidated only by a lockfile change.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Source layer.
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — Python dependency + project install (uv) -> /app/.venv
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS backend

# uv ships as a static binary in its own image; copying it beats curl|sh (no network at build,
# reproducible, no cert/proxy surprises).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependency layer: pyproject.toml + uv.lock only. --no-install-project keeps the project
# itself out so this layer survives every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Project layer: installs f126 itself (console script -> /app/.venv/bin/f126).
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Stage 3 — runtime (no uv, no node, no build toolchain)
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/skrx7392/f126-race-engineer" \
      org.opencontainers.image.description="F1 25/26 UDP telemetry capture + live pit-wall dashboard" \
      org.opencontainers.image.licenses="MIT"

# Fixed uid/gid so the PVC keeps consistent ownership across image rebuilds.
RUN groupadd --gid 10001 f126 \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin f126

WORKDIR /app

# uv installs the project in editable mode, so /app/src must exist at runtime alongside the
# venv (the .pth in site-packages points back at it). Same /app prefix as the build stage —
# the venv's absolute paths depend on it.
COPY --from=backend --chown=10001:10001 /app/.venv /app/.venv
COPY --from=backend --chown=10001:10001 /app/src /app/src
COPY --from=frontend --chown=10001:10001 /build/dist /app/frontend/dist

# Diagnostic scripts (tools/sniff.py). Tiny, dependency-free, and the difference between
# debugging a silent telemetry feed in-pod and rebuilding an image to get a tool in there.
COPY --chown=10001:10001 tools/ /app/tools/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    F126_STATIC_DIR=/app/frontend/dist \
    F126_DATA_DIR=/data

# Mount point for the PVC. Pre-created and owned so the container works even when the volume
# is provisioned with restrictive permissions.
RUN mkdir -p /data && chown 10001:10001 /data
VOLUME ["/data"]

USER 10001:10001

EXPOSE 8000/tcp
EXPOSE 20777/udp

ENTRYPOINT ["/app/.venv/bin/f126", "serve"]
