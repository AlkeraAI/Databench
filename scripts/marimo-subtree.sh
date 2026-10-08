#!/usr/bin/env bash
# Maintain vendor/marimo, a squashed git subtree of marimo at a release tag.
#
# Upstream's tree is filtered before it is squashed: its docs (about 160 MB of
# images and videos), frontend and examples directories are dropped, because
# Alkera never serves marimo's frontend and the repository should not carry the
# weight. The filtered commit is built deterministically (fixed author,
# committer, dates and message), so any checkout regenerates the exact object
# that the last squash commit's `git-subtree-split:` trailer names, and
# `git subtree merge` can diff the old base against the new one for a bump.
#
# Usage:
#   scripts/marimo-subtree.sh fetch            # fetch tags, list releases newer than the base
#   scripts/marimo-subtree.sh add TAG          # first import (vendor/marimo must not exist)
#   scripts/marimo-subtree.sh bump TAG         # move the base to TAG, keeping Alkera edits
#   scripts/marimo-subtree.sh filtered TAG     # print the filtered commit for TAG
set -euo pipefail

REMOTE=marimo-upstream
URL=https://github.com/marimo-team/marimo.git
PREFIX=vendor/marimo
EXCLUDED=(docs frontend examples)
# Upstream tags live in their own ref namespace, not among this repo's tags.
NS=refs/marimo-upstream/tags

ensure_remote() {
  git remote get-url "$REMOTE" >/dev/null 2>&1 || git remote add "$REMOTE" "$URL"
}

fetch_tag() {
  local tag=$1
  if ! git rev-parse -q --verify "$NS/$tag^{commit}" >/dev/null; then
    ensure_remote
    git fetch --no-tags "$REMOTE" "refs/tags/$tag:$NS/$tag"
  fi
}

filtered_commit() {
  local tag=$1 upstream tree date
  fetch_tag "$tag"
  upstream=$(git rev-parse "$NS/$tag^{commit}")
  tree=$(git ls-tree "$upstream" | awk -F '\t' -v drop=" ${EXCLUDED[*]} " 'index(drop, " " $2 " ") == 0' | git mktree)
  date=$(git log -1 --format=%cI "$upstream")
  GIT_AUTHOR_NAME="marimo upstream" GIT_AUTHOR_EMAIL="noreply@marimo.io" GIT_AUTHOR_DATE="$date" \
  GIT_COMMITTER_NAME="marimo upstream" GIT_COMMITTER_EMAIL="noreply@marimo.io" GIT_COMMITTER_DATE="$date" \
    git commit-tree "$tree" -m "marimo $tag without ${EXCLUDED[*]}" -m "upstream: $upstream"
}

base_tag() {
  sed -n 's/^Base tag: `\([^`]*\)`.*/\1/p' "$PREFIX/README.alkera.md" | head -1
}

case "${1:-}" in
  fetch)
    ensure_remote
    git fetch --no-tags "$REMOTE" "+refs/tags/*:$NS/*" >/dev/null
    base=$(base_tag)
    echo "Base: $base"
    echo "Newer releases:"
    git for-each-ref --format='%(refname:lstrip=3)' "$NS" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' \
      | sort -V | awk -v b="$base" 'f; $0==b {f=1}'
    ;;
  filtered)
    filtered_commit "$2"
    ;;
  add)
    test ! -e "$PREFIX" || { echo "$PREFIX exists; use bump" >&2; exit 1; }
    commit=$(filtered_commit "$2")
    git subtree add --prefix="$PREFIX" "$commit" --squash -m "vendor: Import marimo $2 as a squashed subtree"
    ;;
  bump)
    old=$(base_tag)
    test -n "$old" || { echo "no base tag in $PREFIX/README.alkera.md" >&2; exit 1; }
    filtered_commit "$old" >/dev/null   # the object the last squash names must exist locally
    commit=$(filtered_commit "$2")
    git subtree merge --prefix="$PREFIX" "$commit" --squash -m "vendor: Bump marimo from $old to $2"
    echo "Now update the base tag in $PREFIX/README.alkera.md, re-run make gen-alkera-marimo and the nbfmt tests."
    ;;
  *)
    sed -n '2,16p' "$0" >&2
    exit 2
    ;;
esac
