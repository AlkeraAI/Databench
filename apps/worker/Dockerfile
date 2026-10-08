# Databench worker: the Temporal workflows, activities and schedules.
#
# Built from the repository root (the Databench folder):
#   docker build -f apps/worker/Dockerfile .

# The node bundle architectures the image carries: `native` (the default, this
# build's own), `all`, `amd64` or `arm64`. See the node-bundle stages below.
ARG NODE_BUNDLE_ARCHES=native
ARG TARGETARCH

# ---- Stage 1: builder ----
# Base images are pinned by digest so a re-pushed tag cannot change what ships.
FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280 AS builder

# uv, pinned by tag and digest: it resolves and installs the whole dependency
# closure, so it sits above uv.lock in the trust chain.
COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /uvx /bin/

# psycopg and asyncpg need libpq and a build chain.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1

# The same workdir in both stages, so uv's editable .pth files (absolute paths
# to the workspace packages) stay valid after the runtime COPY.
WORKDIR /opt/alkera

# The dependency closure first, from the lockfile and the members' manifests
# alone, so its layer is reused by every build whose lock and manifests did not
# change. `--package alkera-worker` installs only this service's closure.
COPY pyproject.toml uv.lock README.md ./
COPY apps/backend/pyproject.toml ./apps/backend/
COPY apps/worker/pyproject.toml ./apps/worker/
COPY apps/cli/pyproject.toml ./apps/cli/
COPY apps/model-gateway/pyproject.toml ./apps/model-gateway/
COPY packages/api-core/pyproject.toml ./packages/api-core/
COPY packages/py-sdk/pyproject.toml ./packages/py-sdk/
COPY packages/alkera-py/pyproject.toml ./packages/alkera-py/
COPY packages/alkera-kernel/pyproject.toml ./packages/alkera-kernel/
COPY packages/alkera-notebook/pyproject.toml ./packages/alkera-notebook/
COPY packages/test-support/pyproject.toml ./packages/test-support/
RUN uv sync --frozen --no-dev --package alkera-worker --no-install-workspace
COPY apps/backend ./apps/backend
COPY apps/worker ./apps/worker
COPY apps/cli ./apps/cli
COPY apps/model-gateway ./apps/model-gateway
COPY packages/api-core ./packages/api-core
COPY packages/py-sdk ./packages/py-sdk
COPY packages/alkera-py ./packages/alkera-py
COPY packages/alkera-kernel ./packages/alkera-kernel
COPY packages/alkera-notebook ./packages/alkera-notebook
COPY packages/test-support ./packages/test-support

RUN uv sync --frozen --no-dev --package alkera-worker

# ---- The node bundles machines install, one per architecture ----
# Built from source by scripts/build-node-bundle.sh (the CLI on a relocatable
# CPython with the opencode harness); never downloaded from a release. The
# backend serves them to machines and the worker's SSH provider uploads the one
# matching each host's `uname -m`. Everything runs inside these stages, so a
# Windows, macOS or Linux host builds them alike. NODE_BUNDLE_ARCHES picks which
# the image carries: `native` (the default) builds only this image's own
# architecture; `all` builds both, the other one under emulation, which Docker
# must be able to run (see deploy/INSTALL.md). BuildKit builds only the stages
# it names.
FROM --platform=linux/amd64 python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280 AS node-bundle-amd64

# "exec format error" here: Docker cannot run linux/amd64. Enable emulation
# (deploy/INSTALL.md) or build with NODE_BUNDLE_ARCHES=native.
RUN echo "linux/amd64 runs here; if not, enable emulation (deploy/INSTALL.md) or set NODE_BUNDLE_ARCHES=native"
# make, g++ and python3 for the harness's native addons (node-gyp).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git unzip make g++ python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.20@sha256:eaa5f1a3305307aaf9e67fe2bbba1d85ebbb2d8a63bce23af21797bfafbe0f8b /uv /uvx /bin/
COPY --from=oven/bun:1.3.14@sha256:e10577f0db68676a7024391c6e5cb4b879ebd17188ab750cf10024a6d700e5c4 /usr/local/bin/bun /usr/local/bin/bun

WORKDIR /src
COPY . .
ARG APP_VERSION=0.0.0
ARG BUILD_ID=source
# The harness build reads two facts from git, which an image build does not
# copy: the vendor tree's hash (the build id stands in, unique to this build)
# and the release channel opencode stamps its version with.
RUN ALKERA_OPENCODE_SHA="build-${BUILD_ID}" OPENCODE_CHANNEL=node-bundle \
    bash scripts/build-node-bundle.sh --out /out --version "${APP_VERSION}+${BUILD_ID}"

FROM --platform=linux/arm64 python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280 AS node-bundle-arm64

# "exec format error" here: Docker cannot run linux/arm64. Enable emulation
# (deploy/INSTALL.md) or build with NODE_BUNDLE_ARCHES=native.
RUN echo "linux/arm64 runs here; if not, enable emulation (deploy/INSTALL.md) or set NODE_BUNDLE_ARCHES=native"
# make, g++ and python3 for the harness's native addons (node-gyp).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git unzip make g++ python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.20@sha256:eaa5f1a3305307aaf9e67fe2bbba1d85ebbb2d8a63bce23af21797bfafbe0f8b /uv /uvx /bin/
COPY --from=oven/bun:1.3.14@sha256:e10577f0db68676a7024391c6e5cb4b879ebd17188ab750cf10024a6d700e5c4 /usr/local/bin/bun /usr/local/bin/bun

WORKDIR /src
COPY . .
ARG APP_VERSION=0.0.0
ARG BUILD_ID=source
# The harness build reads two facts from git, which an image build does not
# copy: the vendor tree's hash (the build id stands in, unique to this build)
# and the release channel opencode stamps its version with.
RUN ALKERA_OPENCODE_SHA="build-${BUILD_ID}" OPENCODE_CHANNEL=node-bundle \
    bash scripts/build-node-bundle.sh --out /out --version "${APP_VERSION}+${BUILD_ID}"

FROM scratch AS node-bundles-amd64
COPY --from=node-bundle-amd64 /out /out

FROM scratch AS node-bundles-arm64
COPY --from=node-bundle-arm64 /out /out

# Both bundles in one directory under one manifest; the merge refuses two
# builds of different versions.
FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280 AS node-bundles-all
COPY scripts/merge-node-bundles.py /merge-node-bundles.py
COPY --from=node-bundle-amd64 /out /parts/amd64
COPY --from=node-bundle-arm64 /out /parts/arm64
RUN python3 -I /merge-node-bundles.py /out /parts/amd64 /parts/arm64

FROM node-bundles-${TARGETARCH} AS node-bundles-native

FROM node-bundles-${NODE_BUNDLE_ARCHES} AS node-bundle

# ---- Stage 2: runtime ----
FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 alkera \
    && useradd --system --uid 1000 --gid alkera --home /opt/alkera --shell /usr/sbin/nologin alkera

COPY --from=builder --chown=alkera:alkera /opt/alkera /opt/alkera
COPY --from=node-bundle --chown=alkera:alkera /out /opt/alkera-node-bundle

WORKDIR /opt/alkera/apps/worker
USER 1000

ARG APP_VERSION=0.0.0
ARG BUILD_ID=unknown
ENV PATH="/opt/alkera/.venv/bin:$PATH" \
    NODE_BUNDLE_DIR=/opt/alkera-node-bundle \
    APP_ENV=production \
    WORKER_MODULE=worker \
    APP_VERSION=${APP_VERSION} \
    BUILD_ID=${BUILD_ID}

LABEL org.opencontainers.image.title="databench-worker" \
      org.opencontainers.image.source="https://github.com/AlkeraAI/Databench" \
      org.opencontainers.image.licenses="Apache-2.0"

# The liveness server binds every interface so the HEALTHCHECK, an orchestrator
# probe and a sidecar all reach it. The probe answers GET /health/live without
# loading the worker runtime. WORKER_MODULE is the module the Makefile and
# dev-all.sh run; a composing image sets its own.
EXPOSE 9000
ENV ALKERA_WORKER_HEALTH_HOST=0.0.0.0
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["sh", "-c", "exec python -m \"$WORKER_MODULE\" health"]

# Serves the queues in ALKERA_TEMPORAL_TASK_QUEUES (all four by default); a
# per-queue deployment passes `--queues <one>` instead.
CMD ["sh", "-c", "exec python -m \"$WORKER_MODULE\" run"]
