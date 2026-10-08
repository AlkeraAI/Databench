#!/usr/bin/env bash
# Refresh the vendored free/personal email-provider domain list.
#
# Downloads a maintained "free email provider domains" list, normalizes it
# (lowercase, sorted, de-duped, comments/blanks stripped), and rewrites the
# committed snapshot at
#   apps/backend/backend/auth/data/free_email_domains.txt
#
# This is a DELIBERATE, reviewable refresh; the app never fetches at runtime.
# Re-run + commit the diff when you want to pull in upstream additions.
#
# Source: tbrianjones/5992856, free email PROVIDERS only (no disposable noise).
# https://gist.github.com/tbrianjones/5992856

set -euo pipefail

cd "$(dirname "$0")/../.."

SOURCE_URL="https://gist.githubusercontent.com/tbrianjones/5992856/raw/free_email_provider_domains.txt"
OUT="apps/backend/backend/auth/data/free_email_domains.txt"
RETRIEVED="$(date -u +%Y-%m-%d)"

mkdir -p "$(dirname "$OUT")"

tmp_raw="$(mktemp)"
tmp_norm="$(mktemp)"
trap 'rm -f "$tmp_raw" "$tmp_norm"' EXIT

echo "downloading $SOURCE_URL"
curl -fsSL "$SOURCE_URL" -o "$tmp_raw"

# Normalize: strip CRs + whitespace, lowercase, drop blanks/comments, dedupe, sort.
tr -d '\r' < "$tmp_raw" \
  | sed 's/[[:space:]]//g' \
  | tr '[:upper:]' '[:lower:]' \
  | grep -v '^#' \
  | grep -v '^$' \
  | sort -u > "$tmp_norm"

count="$(wc -l < "$tmp_norm" | tr -d '[:space:]')"

# Sanity gate: a real list must contain the obvious providers and be large.
for d in gmail.com yahoo.com hotmail.com outlook.com; do
  grep -qx "$d" "$tmp_norm" || { echo "ERROR: normalized list missing $d, aborting" >&2; exit 1; }
done
if [ "$count" -lt 1000 ]; then
  echo "ERROR: only $count domains parsed (expected >1000), aborting" >&2
  exit 1
fi

{
  echo "# Free / personal email-provider domains (one per line, lowercased)."
  echo "# Used by alkera_core.utils.email.is_personal_email to flag non-business signups."
  echo "#"
  echo "# GENERATED — do not hand-edit. Regenerate via: make gen-free-email-domains"
  echo "# Source: $SOURCE_URL"
  echo "# Retrieved: $RETRIEVED   Count: $count"
  echo "#"
  echo "# Modern providers the upstream list predates are added in code via"
  echo "# alkera_core.utils.email._EXTRA_PERSONAL_DOMAINS (e.g. proton.me, pm.me)."
  cat "$tmp_norm"
} > "$OUT"

echo "wrote $OUT ($count domains)"
