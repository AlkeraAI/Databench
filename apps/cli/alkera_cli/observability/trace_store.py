"""Trace retention: delete whole chat sessions past the retention window."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from alkera_core.project.chats.store import ChatStore
from alkera_core.schemas.chat.manifest import ChatManifest

logger = logging.getLogger(__name__)


class _Index(NamedTuple):
    by_id: dict[str, ChatManifest]
    children: dict[str, list[str]]
    roots: list[str]


def _last_activity(manifest: ChatManifest) -> datetime | None:
    stamp = manifest.updated_at or manifest.created_at
    if stamp is not None and stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp


def _own_directory_only(store: ChatStore, summaries: list[ChatManifest]) -> list[ChatManifest]:
    """Keep only the manifests that unambiguously name their own chat directory.

    A manifest carries its session id in its *contents*, and that id is what this
    module hands to ``ChatStore.delete``, which joins it onto the chats directory
    and removes the result recursively. So a chat directory shipping a manifest
    that says ``"session_id": "../../.."`` — or an absolute path, which drops the
    left operand of the join entirely — picks the directory that gets deleted,
    and one naming a *sibling* chat gets that sibling deleted when the impostor
    expires. Neither needs an agent turn: a repository can carry a tracked
    ``.alkera/chats/<anything>/manifest.json``, and the sweep runs on the first
    chat opened after the clone.

    The directory listing is therefore the authority on identity: an id is
    sweepable only when a directory of exactly that name exists and exactly one
    manifest claims it. Everything else is left in place — skipping a chat costs
    disk, deleting the wrong tree costs the user's data.
    """
    on_disk = set(store.list_session_ids())
    claims = Counter(manifest.session_id for manifest in summaries)
    kept: list[ChatManifest] = []
    for manifest in summaries:
        session_id = manifest.session_id
        if session_id in on_disk and claims[session_id] == 1:
            kept.append(manifest)
            continue
        logger.warning(
            "trace sweep: ignoring a chat whose manifest claims the session id %r; "
            "only a manifest naming its own directory is swept",
            session_id,
        )
    return kept


def _index(summaries: list[ChatManifest]) -> _Index:
    known = {m.session_id for m in summaries}
    by_id: dict[str, ChatManifest] = {}
    children: dict[str, list[str]] = {}
    for manifest in summaries:
        by_id[manifest.session_id] = manifest
        parent = manifest.parent_session_id
        if parent in known:
            children.setdefault(parent, []).append(manifest.session_id)
    roots = [m.session_id for m in summaries if m.parent_session_id not in known]
    return _Index(by_id, children, roots)


def _subtree_expired(session_id: str, index: _Index, cutoff: datetime) -> bool:
    """A session is expendable only when it AND every descendant are idle past
    the cutoff with no background job running."""
    manifest = index.by_id[session_id]
    stamp = _last_activity(manifest)
    if stamp is None or stamp >= cutoff or manifest.background_jobs_running:
        return False
    children = index.children.get(session_id, [])
    return all(_subtree_expired(child, index, cutoff) for child in children)


def _inside_store(store: ChatStore, session_id: str) -> bool:
    """True when ``chats/<session_id>`` really is a direct subdirectory of the store.

    Belt and braces on top of the identity check: a chat directory can also be a
    symlink, in which case the name is legitimate but the tree that would be
    removed lives somewhere else entirely.
    """
    try:
        root = store.path.resolve()
        target = (store.path / session_id).resolve()
    except OSError:
        return False
    return target != root and target.parent == root


def _delete_one(store: ChatStore, session_id: str) -> bool:
    if not _inside_store(store, session_id):
        logger.warning(
            "trace sweep: refusing to delete %r — it does not resolve to a chat inside %s",
            session_id,
            store.path,
        )
        return False
    try:
        store.delete(session_id)
    except Exception:  # locked or already gone; the next sweep retries
        logger.debug("trace sweep: skipped %s", session_id, exc_info=True)
        return False
    return True


def _delete_subtree(store: ChatStore, session_id: str, index: _Index) -> bool:
    """Delete an expired session and its descendants, children first.

    The recursion happens here rather than through ``store.delete(recursive=True)``
    because that path re-derives the child ids from manifest *contents*: a chat
    directory that merely claims an expiring session as its parent would otherwise
    choose the next directory removed, which is exactly what the identity check
    above exists to prevent. Every id deleted here was matched against a real
    chat directory first.

    A descendant that cannot be removed — an active writer holds its lock — keeps
    its ancestors, so no chat is left parented to a directory that is gone; the
    next sweep retries the whole subtree.
    """
    complete = True
    for child in index.children.get(session_id, []):
        complete = _delete_subtree(store, child, index) and complete
    if not complete:
        logger.debug("trace sweep: kept %s until its children are gone", session_id)
        return False
    return _delete_one(store, session_id)


def sweep_expired_traces(
    store: ChatStore, *, retention_days: int, now: datetime | None = None
) -> int:
    """Delete root sessions whose whole subtree is expendable; return how many.

    ``retention_days <= 0`` disables the sweep."""
    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    try:
        summaries = _own_directory_only(store, store.list_summaries())
    except Exception:
        logger.warning("trace sweep: could not list chats", exc_info=True)
        return 0
    index = _index(summaries)
    expendable = [sid for sid in index.roots if _subtree_expired(sid, index, cutoff)]
    removed = sum(1 for sid in expendable if _delete_subtree(store, sid, index))
    if removed:
        logger.info("trace sweep: removed %d expired session(s)", removed)
    return removed
