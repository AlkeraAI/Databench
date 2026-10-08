# syntax=docker/dockerfile:1.7-labs
# Databench web: the SPA bundle served by nginx, with /api and /health
# reverse-proxied to the backend container.
#
# Built from the repository root (the Databench folder):
#   docker build -f apps/web/Dockerfile .
#
# Runtime env:
#   ALKERA_BACKEND_URL      the upstream backend (for example http://backend:8000),
#                           substituted into nginx.conf at container start.
#   FILES_CONTENT_BASE_URL  the Files content origin, added to the CSP when set.

# ---- Stage 1: build the Vite bundle ----
# Base images are pinned by digest so a re-pushed tag cannot change what ships.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS builder

RUN corepack enable

WORKDIR /build

# The lockfile and every workspace manifest first, so the install layer is
# reused while they do not change.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
# Every workspace package's manifest, wherever it sits: the install resolves
# the whole workspace against the lockfile.
COPY --parents apps/*/package.json packages/*/package.json ./
COPY packages/shared-openapi/README.md ./packages/shared-openapi/README.md
COPY packages/shared-openapi/openapi.json ./packages/shared-openapi/openapi.json

RUN pnpm install --frozen-lockfile --filter "@alkera/web..."

# Sources after the install, so a source change keeps the node_modules layer.
COPY apps/web ./apps/web
COPY packages ./packages
RUN pnpm --filter @alkera/web build

# ---- Stage 2: nginx serves the bundle and reverse-proxies the API ----
# Non-root nginx (uid 101) on 8080; it also runs under an arbitrary injected UID.
FROM nginxinc/nginx-unprivileged:1.29-alpine@sha256:0c79d56aee561a1d81c63f00eee5fb5fe29279560cdc55e91425133104c7fbe6

USER root
# gettext brings envsubst for the entrypoint; apk upgrade applies the Alpine
# security patches released since the base image was cut.
RUN apk upgrade --no-cache && apk add --no-cache gettext

COPY apps/web/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY apps/web/nginx.gateway.template /etc/nginx/templates/gateway.location.template
COPY apps/web/nginx.content.template /etc/nginx/templates/content.server.template
COPY apps/web/docker-entrypoint.sh /docker-entrypoint.d/40-alkera-render-config.sh
RUN chmod +x /docker-entrypoint.d/40-alkera-render-config.sh

COPY --from=builder /build/apps/web/dist /usr/share/nginx/html

ENV ALKERA_BACKEND_URL=http://backend:8000

USER 101
EXPOSE 8080

LABEL org.opencontainers.image.title="databench-web" \
      org.opencontainers.image.source="https://github.com/AlkeraAI/Databench" \
      org.opencontainers.image.licenses="Apache-2.0"
