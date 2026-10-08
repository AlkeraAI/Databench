# Databench box: the image a compute box runs, with the `alkera` CLI and
# daemon installed from the workspace's wheels and the opencode harness built
# from vendor/opencode with bun.
#
# Built from the repository root (the Databench folder):
#   docker build -f deploy/docker/box/Dockerfile .
#
# It is the localdev box recipe (deploy/docker/localdev-box: Ubuntu, gVisor,
# the sandbox prerequisites' packages, tini) with the software a box serves
# already inside, so nothing is synced from a source tree at boot:
#   /opt/alkera-venv/bin/alkera        the CLI and daemon (non-editable install)
#   /opt/alkera-agent/opencode, rg     the harness and its ripgrep
# ALKERA_OPENCODE_BIN and ALKERA_RIPGREP_BIN point the daemon at them.
#
# Every pinned version is read from its one owner while building: bun from
# vendor/opencode/package.json, ripgrep from scripts/build-opencode-binary.sh,
# the Python packages from uv.lock.

# ---- Stage 1: the CLI's wheels and its locked dependency closure ----
FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280 AS wheels

COPY --from=ghcr.io/astral-sh/uv:0.11.20@sha256:eaa5f1a3305307aaf9e67fe2bbba1d85ebbb2d8a63bce23af21797bfafbe0f8b /uv /bin/uv

ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY apps ./apps
COPY packages ./packages

# The hash-checked requirements of the CLI's closure (workspace members
# excluded), then a wheel for every workspace member the CLI imports.
RUN mkdir -p /out/wheels \
    && uv export --frozen --no-dev --package alkera-cli --no-emit-workspace \
        --output-file /out/requirements.txt \
    && for member in apps/cli packages/api-core packages/py-sdk packages/alkera-py \
            packages/alkera-kernel packages/alkera-notebook; do \
        uv build --wheel --no-config --out-dir /out/wheels "$member" || exit 1; \
    done

# ---- Stage 2: the opencode harness, built with bun ----
FROM debian:bookworm-slim AS harness

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip jq git python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

COPY vendor/opencode /build
COPY scripts/build-opencode-binary.sh /tmp/build-opencode-binary.sh

ARG TARGETARCH
# Native addons (tree-sitter grammars) build through node-gyp.
RUN set -eu; \
    bun_version="$(jq -r .packageManager /build/package.json | sed 's/^bun@//')"; \
    curl -fsSL https://bun.sh/install | bash -s "bun-v${bun_version}"; \
    export PATH="/root/.bun/bin:$PATH"; \
    bun install -g node-gyp; \
    cd /build && bun install --frozen-lockfile; \
    cd /build/packages/opencode && OPENCODE_CHANNEL=box bun run script/build.ts --single --skip-embed-web-ui --baseline; \
    built="$(find dist -maxdepth 3 -type f -name opencode | head -n1)"; \
    [ -x "$built" ] || { echo "the build produced no opencode binary" >&2; exit 1; }; \
    install -D -m 0755 "$built" /out/opencode; \
    rg_version="$(sed -n 's/^RIPGREP_VERSION="\(.*\)"$/\1/p' /tmp/build-opencode-binary.sh)"; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        arm64) rg_triple=aarch64-unknown-linux-gnu ;; \
        amd64) rg_triple=x86_64-unknown-linux-musl ;; \
        *) echo "no ripgrep build for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 -o /tmp/rg.tar.gz \
        "https://github.com/BurntSushi/ripgrep/releases/download/${rg_version}/ripgrep-${rg_version}-${rg_triple}.tar.gz"; \
    tar -xzf /tmp/rg.tar.gz -C /tmp; \
    install -m 0755 "/tmp/ripgrep-${rg_version}-${rg_triple}/rg" /out/rg; \
    /out/opencode --version

# ---- Stage 3: the box ----
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
# What the node bootstrap and the sandbox prerequisites expect installed, so a
# boot re-checks instead of reinstalling (see deploy/docker/localdev-box).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        uidmap nftables util-linux acl iproute2 bzip2 ca-certificates curl gnupg e2fsprogs quota \
        jq unzip netbase socat git tini \
    && rm -rf /var/lib/apt/lists/*

# gVisor (runsc) from gVisor's apt repository.
RUN install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /etc/apt/keyrings/gvisor-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
        >/etc/apt/sources.list.d/gvisor.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends runsc \
    && rm -rf /var/lib/apt/lists/* \
    && runsc --version

COPY --from=ghcr.io/astral-sh/uv:0.11.20@sha256:eaa5f1a3305307aaf9e67fe2bbba1d85ebbb2d8a63bce23af21797bfafbe0f8b /uv /uvx /usr/local/bin/

# A uv-managed CPython the venv owns outright, then the locked closure
# (hash-checked) and the workspace wheels on top of it, both without
# resolving: the closure is the lock's resolution, overrides included, and a
# second resolve here would not see the workspace's overrides.
COPY --from=wheels /out /tmp/alkera
RUN export UV_PYTHON_INSTALL_DIR=/opt/alkera-python \
    && uv python install 3.13 \
    && uv venv --python 3.13 --managed-python /opt/alkera-venv \
    && uv pip install --no-config --python /opt/alkera-venv/bin/python \
        --require-hashes --no-deps -r /tmp/alkera/requirements.txt \
    && uv pip install --no-config --python /opt/alkera-venv/bin/python --no-deps /tmp/alkera/wheels/*.whl \
    && chmod -R go+rX /opt/alkera-python /opt/alkera-venv \
    && rm -rf /tmp/alkera \
    && /opt/alkera-venv/bin/alkera --version

COPY --from=harness /out/opencode /out/rg /opt/alkera-agent/
# Registers the box as a chat machine for a stack and serves its chats (the
# compose `box` service runs it); the same script `make dev-chat-local` runs.
COPY ops/scripts/dev/chat-local-machine.sh /opt/alkera-box/chat-local-machine.sh

ENV PATH="/opt/alkera-venv/bin:$PATH" \
    ALKERA_OPENCODE_BIN=/opt/alkera-agent/opencode \
    ALKERA_RIPGREP_BIN=/opt/alkera-agent/rg

LABEL org.opencontainers.image.title="databench-box" \
      org.opencontainers.image.source="https://github.com/AlkeraAI/Databench" \
      org.opencontainers.image.licenses="Apache-2.0"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["alkera", "--help"]
