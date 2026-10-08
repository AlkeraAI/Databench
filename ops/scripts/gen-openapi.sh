#!/usr/bin/env bash
# Dump the open app's OpenAPI document to packages/shared-openapi/open/openapi.json,
# or, with --app MODULE:ATTR, that composition root's to
# packages/shared-openapi/openapi.json (--out names another file).
#
# Runs inside apps/backend so imports resolve; scripts/export_openapi.py picks
# the app and writes the file.

set -euo pipefail

cd "$(dirname "$0")/../.."

(
  cd apps/backend
  uv run python ../../scripts/export_openapi.py "$@"
)
