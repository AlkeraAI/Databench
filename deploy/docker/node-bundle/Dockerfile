# The node bundle for this image's architecture, built from source
# (scripts/build-node-bundle.sh). An open service image takes it as a stage:
#
#   FROM <this file's stages> AS node-bundle        (or build it with
#   COPY --from=node-bundle /out /opt/alkera-node-bundle    --build-context)
#   ENV NODE_BUNDLE_DIR=/opt/alkera-node-bundle
#
# Built from the repository root: docker build -f deploy/docker/node-bundle/Dockerfile .
FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280 AS node-bundle

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git unzip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /uvx /bin/
COPY --from=oven/bun:1.3.14 /usr/local/bin/bun /usr/local/bin/bun

WORKDIR /src
COPY . .
ARG APP_VERSION=0.0.0
ARG BUILD_ID=source
RUN bash scripts/build-node-bundle.sh --out /out --version "${APP_VERSION}+${BUILD_ID}"
