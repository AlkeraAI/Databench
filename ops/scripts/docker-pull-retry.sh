#!/usr/bin/env bash
# Acquire container images, retrying a registry that refuses.
#
# GitHub's `services:` containers are pulled in the runner's "Initialize
# containers" phase, which runs BEFORE the first step and has no retry of any
# kind. One refused pull there ends the job, `/usr/bin/docker pull
# trinodb/trino … Process completed with exit code 125`, before a single step
# exists to catch it, so neither `continue-on-error`, nor a retry action, nor
# anything a test could allow for can see it. The CI jobs that need local
# services therefore start their containers from a STEP and acquire the images
# through here, where a refusal is ours to handle.
#
# Registries refuse in two shapes that want different answers. A 5xx, a reset
# TLS handshake or a truncated layer clears in seconds; that is what the
# exponential backoff is for. Docker Hub's anonymous pull-rate allowance, keyed
# to the runner's shared egress IP, does not clear for hours; no amount of
# retrying wins that one, so when DOCKERHUB_USERNAME/DOCKERHUB_TOKEN are present
# the pull is authenticated first, against the far larger per-account allowance.
# Both credentials absent is the supported default: the pull just goes out
# anonymously, exactly as it does today.
#
# Usage: docker-pull-retry.sh IMAGE [IMAGE...]
#
#   DOCKER_PULL_ATTEMPTS      attempts per image           (default 5)
#   DOCKER_PULL_BACKOFF       seconds before the 2nd try,  (default 5)
#                             doubled each further try
#   DOCKER_PULL_BACKOFF_MAX   cap on that wait             (default 40)
#   DOCKER_BIN                docker executable            (default "docker")
#
# A backoff of 0 disables the wait, which is how the tests drive the retry
# ladder without spending wall-clock on it.
set -euo pipefail

DOCKER_BIN="${DOCKER_BIN:-docker}"
ATTEMPTS="${DOCKER_PULL_ATTEMPTS:-5}"
BACKOFF="${DOCKER_PULL_BACKOFF:-5}"
BACKOFF_MAX="${DOCKER_PULL_BACKOFF_MAX:-40}"

log() { echo "[docker-pull-retry] $*" >&2; }

[ "$#" -gt 0 ] || { log "usage: docker-pull-retry.sh IMAGE [IMAGE...]"; exit 2; }

# One login for the whole invocation, and only when BOTH halves are set, a
# half-configured secret must not turn an anonymous pull that would have worked
# into a login failure, so a refused login is a warning and the pull proceeds
# unauthenticated.
if [ -n "${DOCKERHUB_USERNAME:-}" ] && [ -n "${DOCKERHUB_TOKEN:-}" ]; then
  if printf '%s' "$DOCKERHUB_TOKEN" \
    | "$DOCKER_BIN" login --username "$DOCKERHUB_USERNAME" --password-stdin >/dev/null 2>&1; then
    log "authenticated to Docker Hub as $DOCKERHUB_USERNAME"
  else
    log "WARNING: Docker Hub login failed; pulling anonymously"
  fi
fi

failed=0
for image in "$@"; do
  # An image already on the host is not re-resolved. This is not only speed:
  # Docker Hub charges the manifest request of an up-to-date `pull` against the
  # same rate allowance as a real download, so re-checking a pinned image spends
  # the very budget these refusals are about.
  if "$DOCKER_BIN" image inspect "$image" >/dev/null 2>&1; then
    log "$image already present"
    continue
  fi

  wait_s="$BACKOFF"
  attempt=1
  pulled=""
  while [ "$attempt" -le "$ATTEMPTS" ]; do
    # docker's progress goes to stderr with the rest of this script's chatter:
    # airflow-test-up.sh runs this and its stdout is appended to $GITHUB_ENV,
    # where a "Pulling from ..." line is an invalid assignment.
    if "$DOCKER_BIN" pull "$image" >&2; then
      pulled=1
      break
    fi
    log "pull of $image failed (attempt $attempt/$ATTEMPTS)"
    if [ "$attempt" -lt "$ATTEMPTS" ]; then
      if [ "$wait_s" -gt 0 ]; then
        log "retrying in ${wait_s}s"
        sleep "$wait_s"
      fi
      wait_s=$(( wait_s * 2 ))
      [ "$wait_s" -le "$BACKOFF_MAX" ] || wait_s="$BACKOFF_MAX"
    fi
    attempt=$(( attempt + 1 ))
  done

  if [ -n "$pulled" ]; then
    log "$image pulled on attempt $attempt"
  else
    log "ERROR: $image could not be pulled in $ATTEMPTS attempts"
    failed=1
  fi
done

exit "$failed"
