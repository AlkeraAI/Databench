# Databench model gateway: an authenticated proxy to the LLM providers.
#
# Built from the repository root (the Databench folder):
#   docker build -f apps/model-gateway/Dockerfile .
#
# The open build meters nothing: requests pass through with the operator's own
# provider keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, or Bedrock through the
# standard AWS credential chain).

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
# change. `--package alkera-model-gateway` installs only this service's closure.
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
RUN uv sync --frozen --no-dev --package alkera-model-gateway --no-install-workspace
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

RUN uv sync --frozen --no-dev --package alkera-model-gateway

# tiktoken's BPE ranks are baked in: the token estimator runs on the admission
# path, and tiktoken otherwise downloads them on first use, synchronously and
# with no timeout, which stalls every egress-restricted install. The download
# runs here through tiktoken itself, so the cache file names are its own. The
# names stay in step with ``ENCODING_NAMES`` in ``model_gateway/estimate.py``;
# ``apps/model-gateway/tests/test_token_estimate.py`` fails if they drift.
ENV TIKTOKEN_CACHE_DIR=/opt/alkera/var/tiktoken
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" \
    && /opt/alkera/.venv/bin/python -c "import tiktoken; [tiktoken.get_encoding(n) for n in ('o200k_base', 'cl100k_base')]" \
    && test -n "$(ls -A "$TIKTOKEN_CACHE_DIR")"

# ---- Stage 2: runtime ----
FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 alkera \
    && useradd --system --uid 1000 --gid alkera --home /opt/alkera --shell /usr/sbin/nologin alkera

COPY --from=builder --chown=alkera:alkera /opt/alkera /opt/alkera

WORKDIR /opt/alkera/apps/model-gateway
USER 1000

ARG APP_VERSION=0.0.0
ARG BUILD_ID=unknown
# TIKTOKEN_CACHE_DIR points at the ranks baked in the builder stage.
ENV PATH="/opt/alkera/.venv/bin:$PATH" \
    APP_ENV=production \
    TIKTOKEN_CACHE_DIR=/opt/alkera/var/tiktoken \
    GATEWAY_APP="--factory model_gateway.app_factory:create_app" \
    APP_VERSION=${APP_VERSION} \
    BUILD_ID=${BUILD_ID}

EXPOSE 8081

LABEL org.opencontainers.image.title="databench-gateway" \
      org.opencontainers.image.source="https://github.com/AlkeraAI/Databench" \
      org.opencontainers.image.licenses="Apache-2.0"

# GATEWAY_APP is uvicorn's app argument; the open factory installs no
# extension, so the gateway meters nothing.
# --timeout-graceful-shutdown: the gateway relays provider streams that belong
# to chat turns, so a stop keeps serving open streams for most of the
# orchestrator's stop timeout and then closes them itself.
# --no-access-log: uvicorn's own access line prints the raw query string; the
# app's `http.access` event logs every request with credentials scrubbed.
CMD ["sh", "-c", "exec uvicorn $GATEWAY_APP --host 0.0.0.0 --port 8081 --timeout-graceful-shutdown 110 --no-access-log"]
