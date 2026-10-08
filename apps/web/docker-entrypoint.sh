#!/bin/sh
# Renders /etc/nginx/templates/default.conf.template -> /etc/nginx/conf.d/default.conf
# with ALKERA_BACKEND_URL + ALKERA_CSP substituted, and the gateway's location
# when ALKERA_GATEWAY_UPSTREAM is set. Picked up by the official
# nginx entrypoint (which runs every script in /docker-entrypoint.d/ in order).

set -eu

: "${ALKERA_BACKEND_URL:?ALKERA_BACKEND_URL must be set (e.g. http://backend:8000)}"

# Alkera Files serves uploaded bytes from their own registrable domain — the
# production validator refuses a content origin sharing the app's host, because a
# cookie is scoped by hostname and would ride along with attacker-uploaded
# content. So on a deployment that runs Files EVERY preview is cross-origin: the
# readers fetch the signed URL with CORS (connect-src), a stored PDF or HTML page
# renders in an iframe served from it (frame-src), and audio and video stream
# from it (media-src). A policy that does not name the origin blocks all three,
# so the default learns it from the same FILES_CONTENT_BASE_URL the API reads.
# Reduced to a bare scheme://host[:port]: a CSP host-source carrying a path or a
# trailing slash matches nothing, and would fail silently.
files_origin=""
case "${FILES_CONTENT_BASE_URL:-}" in
    http://*/* | https://*/*) files_origin=" $(printf '%s' "$FILES_CONTENT_BASE_URL" | cut -d/ -f1-3)" ;;
    http://* | https://*) files_origin=" ${FILES_CONTENT_BASE_URL}" ;;
esac

# Content-Security-Policy for the SPA. Default: a strict same-origin policy — the
# API is reverse-proxied same-origin (connect-src 'self') and fonts/scripts are
# self-hosted — plus the content origin above where Files needs it. connect-src
# also names the websocket schemes explicitly: browsers disagree on whether
# 'self' covers a same-host ws:/wss: connection, and the portal's live channel
# (/api/v1/ws) must not depend on that. Operators enabling Cloudflare Turnstile
# or Sentry (or any external origin) must OVERRIDE ALKERA_CSP to add those hosts
# — an override is taken whole, so it must carry the content origin itself.
# `:=` also backfills an empty value so envsubst never renders an
# (over-restrictive) empty header.
#
# img-src and media-src also name `blob:`: a file preview is fetched from the
# content origin with CORS and handed to the page as a blob URL, so the bytes
# never become a link anyone can reshare — and `blob:` is its own scheme source,
# so without it the browser blocks the picture the app just built from its own
# response. `blob:` stays out of frame-src, script-src and style-src — a blob:
# document runs in the app's own origin, which separating the content host
# exists to prevent.
: "${ALKERA_CSP:=default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; img-src 'self' data: blob:; media-src 'self' blob:${files_origin}; frame-src 'self'${files_origin}; font-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self' ws: wss:${files_origin}}"
export ALKERA_BACKEND_URL ALKERA_CSP

# The image's paths; a test points them at a directory of its own.
templates="${ALKERA_NGINX_TEMPLATES:-/etc/nginx/templates}"
conf_dir="${ALKERA_NGINX_CONF_DIR:-/etc/nginx/conf.d}"

envsubst '${ALKERA_BACKEND_URL} ${ALKERA_CSP}' \
    < "$templates/default.conf.template" \
    > "$conf_dir/default.conf"

# The Files content origin, when FILES_CONTENT_BASE_URL names one: a server for
# that hostname alone that passes /c to the backend, so the operator points the
# content hostname at this container just as they point the app's. The host is
# the URL's host without scheme, port or path; nginx matches server_name on the
# Host header's name only.
# nginx includes conf.d/*.conf in name order, and this server logs with the
# alkera_access format default.conf defines, so its file must sort after
# default.conf or nginx refuses to start.
content_conf="$conf_dir/files-content.conf"
rm -f "$content_conf" "$conf_dir/alkera-content.conf"
content_host="$(printf '%s' "${FILES_CONTENT_BASE_URL:-}" | sed -n 's#^https\{0,1\}://\([^/:]*\).*#\1#p')"
if [ -n "$content_host" ]; then
    ALKERA_CONTENT_HOST="$content_host"
    export ALKERA_CONTENT_HOST
    envsubst '${ALKERA_BACKEND_URL} ${ALKERA_CONTENT_HOST}' \
        < "$templates/content.server.template" \
        > "$content_conf"
fi

# The model gateway at /gateway/ on this origin, for remote machines' nodes,
# when ALKERA_GATEWAY_UPSTREAM names it (http://gateway:8081 in the production
# compose). Unset, no such location exists and /gateway/ is the SPA's.
gateway_conf="$conf_dir/alkera-gateway.location"
rm -f "$gateway_conf"
if [ -n "${ALKERA_GATEWAY_UPSTREAM:-}" ]; then
    ALKERA_GATEWAY_UPSTREAM="${ALKERA_GATEWAY_UPSTREAM%/}"
    # The gateway is resolved per request, not at startup: nginx refuses to start
    # on an upstream host it cannot resolve, and the gateway is a service of its
    # own that may start after this one or not run at all. The resolver is the
    # container's own (Docker's embedded DNS), unless ALKERA_NGINX_RESOLVER names one.
    if [ -z "${ALKERA_NGINX_RESOLVER:-}" ]; then
        ALKERA_NGINX_RESOLVER="$(awk '$1 == "nameserver" { print $2; exit }' \
            "${ALKERA_RESOLV_CONF:-/etc/resolv.conf}")"
        case "$ALKERA_NGINX_RESOLVER" in
            *:*) ALKERA_NGINX_RESOLVER="[$ALKERA_NGINX_RESOLVER]" ;;
        esac
    fi
    if [ -z "$ALKERA_NGINX_RESOLVER" ]; then
        echo "docker-entrypoint: no nameserver for the gateway's upstream; set ALKERA_NGINX_RESOLVER" >&2
        exit 1
    fi
    export ALKERA_GATEWAY_UPSTREAM ALKERA_NGINX_RESOLVER
    envsubst '${ALKERA_GATEWAY_UPSTREAM} ${ALKERA_NGINX_RESOLVER}' \
        < "$templates/gateway.location.template" \
        > "$gateway_conf"
fi
