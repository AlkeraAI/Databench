"""Interning a grant list into one ``file_acls`` row.

An ACL body is content-addressed: the canonical rendering of its grants is
hashed together with the org, and the hash is the unique key. A million nodes
that share one permission set therefore share one row, and the listing join is
one row per distinct permission set rather than one per node.

The org is in the hash because ``uq_file_acls_body_hash`` is global while every
read is filtered to one org, and a body names principals, never the org. A
person who belongs to two orgs gets the same home-folder body in each; hashed
alone, the second org's insert would collide with the first org's row, which
its own read cannot see, and intern nothing
(:meth:`alkera_core.files.acl.AclBody.hash_for` folds the org in the same way).

Interning is ``INSERT … ON CONFLICT (body_hash) DO NOTHING`` followed by a read,
never ``DO UPDATE``. ``DO UPDATE`` would make the row hot and — far worse —
would mutate the meaning of every node already pointing at it, so a body is
written once and is immutable from then on. Two backends interning the same
body concurrently both end up on the same id: the loser's insert affects no
row, and its read blocks on the winner's index entry until that transaction
ends, so it reads the committed row rather than inventing a second one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text

from alkera_core.files.authz.grants import Grant
from alkera_core.files.ids import AclId
from alkera_core.files.repo import FilesRepo


def ace_body(grants: Sequence[Grant]) -> list[dict[str, Any]]:
    """The canonical ACE list for ``grants``.

    Canonical means two callers that mean the same permission set produce
    byte-identical JSON: the rows are sorted by their whole identity and every
    optional field is spelled, so an omitted ``conditions`` and an explicit
    ``null`` cannot hash differently and split one set across two rows.
    """
    rows: list[dict[str, Any]] = []
    for grant in grants:
        origin = grant.origin
        rows.append(
            {
                "principal_kind": grant.principal.kind,
                "principal_id": str(grant.principal.id),
                "role": grant.role,
                "origin": origin.kind,
                "origin_ancestor_id": (
                    None if origin.ancestor_id is None else str(origin.ancestor_id)
                ),
                "expires_at": (None if grant.expires_at is None else grant.expires_at.isoformat()),
                "conditions": (None if grant.conditions is None else dict(grant.conditions)),
            }
        )
    rows.sort(key=_ace_sort_key)
    return rows


def _ace_sort_key(ace: Mapping[str, Any]) -> str:
    """A total order over ACEs that does not depend on dict iteration order."""
    return json.dumps(ace, sort_keys=True, separators=(",", ":"))


def body_hash(body: Sequence[Mapping[str, Any]], *, org_team_id: uuid.UUID) -> str:
    """The interning key: SHA-256 over the canonical JSON of the org and the
    whole body."""
    canonical = json.dumps(
        {"org": str(org_team_id), "aces": list(body)}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def intern(repo: FilesRepo, body: Sequence[Grant]) -> AclId:
    """The id of the ``file_acls`` row holding ``body``, creating it if new.

    Runs in the caller's transaction, so an ACL interned for a folder that is
    then rolled back leaves no orphan row.
    """
    repo._require_open()
    aces = ace_body(body)
    org_id = repo.scope.org_team_id
    digest = body_hash(aces, org_team_id=org_id)
    params = {
        "id": uuid.uuid4(),
        "org": org_id,
        "body": json.dumps(aces),
        "hash": digest,
    }
    await repo.session.execute(
        text(
            "INSERT INTO file_acls (id, org_team_id, body, body_hash) "
            "VALUES (:id, :org, CAST(:body AS jsonb), :hash) "
            "ON CONFLICT (body_hash) DO NOTHING"
        ),
        params,
    )
    found = (
        await repo.session.execute(
            text("SELECT id FROM file_acls WHERE body_hash = :hash AND org_team_id = :org"),
            {"hash": digest, "org": org_id},
        )
    ).scalar_one_or_none()
    if found is None:  # pragma: no cover - only reachable if the row vanished mid-transaction
        raise RuntimeError(f"interned ACL {digest} is not readable in this transaction")
    return AclId(found)


#: ``intern`` is deliberately absent: :func:`alkera_core.files.acl.intern` is the
#: seam callers reach through the package barrel, and a flat barrel can bind one
#: meaning per name. This module's lower-level variant stays reachable as
#: ``acl_intern.intern`` for the drive bootstrap that wants exactly it.
__all__ = ["ace_body", "body_hash"]
