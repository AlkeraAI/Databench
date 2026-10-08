"""Which of a page of nodes the caller may read, in a constant number of queries.

A feed — delta, search, "shared with me" — has to cut its page *after* the
per-item access decision, and the obvious way to do that is a loop that
resolves one node at a time. That loop is an N+1: a page of 10,000
rows becomes 10,000 round trips, and the "delta page in 100 ms" budget cannot
be met or even honestly benchmarked while it stands.

:func:`readable_ids` is the batched shape of the same decision. It loads the
page's nodes and their drives in one statement, those nodes' ancestor chains in
a second, the interned ACL bodies in a third, and the ``file_shares`` rows of
only those chains whose cache is not trustworthy in a fourth; then it runs the
*same* pure :func:`~alkera_core.files.authz.decider.effective_role` per node
over rows that are already in memory. Four statements for any page size, and
an answer that is equal element for element to resolving each node on its own —
which is the property the test pins, because the value of this seam is entirely
in that equality.

The chain is its own statement rather than a join onto the page because of the
role the routes run as: see :func:`_load_page`.

:func:`decided_by_id` is the same load with the rows kept: a surface that
renders what it decided gets the node and its chain out of the statements that
decided them, so it does not go back per row for what it already read.

Only the loading is new. The candidate rules, the ladder, the org-admin
descent, the node flags and the agent confinement are the decider's, unchanged
and unduplicated, and the grant assembly follows
:class:`~alkera_core.files.authz.grants.FilesGrantSource` down to the same
fallback: while a node is ``acl_rewriting`` its cached body is known stale, so
the chain answers instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from alkera_core.authz.principal import ActingContext
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.decider import (
    DEFAULT_DECIDER,
    AccessDecider,
    AccessFacts,
    EffectiveAccess,
    effective_role,
)
from alkera_core.files.authz.grants import (
    ACL_REWRITING,
    Grant,
    GrantOrigin,
    Principal,
    ace_to_grant,
    drive_default_grants,
)
from alkera_core.files.ids import NodeId
from alkera_core.files.path_labels import chain_inos
from alkera_core.files.repo import FilesRepo, ancestor_chain_by_ino, id_batches
from alkera_core.models.files.acl import FileAcl, FileShare
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode


@dataclass(frozen=True, slots=True)
class _Page:
    """The page's rows, already in memory: node, chain and drive per id."""

    nodes: Mapping[uuid.UUID, FileNode]
    chains: Mapping[uuid.UUID, Sequence[FileNode]]
    drives: Mapping[uuid.UUID, FileDrive]


async def _load_page(repo: FilesRepo, wanted: Sequence[NodeId]) -> _Page:
    """Every node of the page with its ancestor chain and its drive, in TWO
    statements.

    The first reads the page's own rows with the drive each sits in. The second
    reads their chains, addressed by ``(drive_id, ino)`` — the labels of a
    node's ``path_ids`` ARE the inos of its ancestors, so the anchors already
    name every row the chain needs and the read is a unique-index lookup rather
    than an ancestry test.

    It is two statements rather than one because of the role the routes run as.
    Asking the ltree question — "a node of the same drive whose ``path_ids``
    contains the anchor's" — planned as a single join, and for the table owner
    it descends ``ix_file_nodes_path_ids``. Under ``alkera_files_app``, where
    ``file_nodes`` carries FORCE row security, it does not: PostgreSQL will not
    promote a non-leakproof qual ahead of a pending security qual, ``subpath()``
    and the ltree containment operators are not leakproof, and the plan falls
    back to reading every node of the drive once per anchor. At a hundred
    thousand nodes that is a ten-second page. Integer and uuid equality are
    leakproof, so the ino spelling keeps its index under the role — which is the
    only role that ever runs it.

    The chain rows come back unordered and are put into root-first order from
    the anchor's own path, which is where the order was written in the first
    place.
    """
    org = repo.scope.org_team_id
    anchors_stmt = (
        repo.select_nodes()
        .join(FileDrive, FileDrive.id == FileNode.drive_id)
        .add_columns(FileDrive)
        .where(FileNode.id.in_(list(wanted)), FileDrive.org_team_id == org)
    )
    nodes: dict[uuid.UUID, FileNode] = {}
    drives: dict[uuid.UUID, FileDrive] = {}
    for node, drive in (await repo.session.execute(anchors_stmt)).all():
        nodes[node.id] = node
        drives[node.id] = drive
    if not nodes:
        return _Page(nodes={}, chains={}, drives={})

    chain_stmt = repo.select_nodes().where(ancestor_chain_by_ino(nodes.values()))
    by_ino: dict[tuple[uuid.UUID, int], FileNode] = {
        (row.drive_id, row.ino): row
        for row in (await repo.session.execute(chain_stmt)).scalars().all()
    }
    chains: dict[uuid.UUID, list[FileNode]] = {}
    for anchor_id, anchor in nodes.items():
        chain = (by_ino.get((anchor.drive_id, ino)) for ino in chain_inos(anchor.path_ids))
        chains[anchor_id] = [row for row in chain if row is not None]
    return _Page(nodes=nodes, chains=chains, drives=drives)


async def _load_acls(
    repo: FilesRepo, page: _Page
) -> tuple[dict[uuid.UUID, FileAcl], set[uuid.UUID]]:
    """The interned bodies the page's trustworthy caches point at, in ONE
    statement, plus the anchors that have to fall back to their chain.

    An anchor falls back when it is being rewritten, when it has no cache at
    all, or when the row its ``acl_id`` names is gone — the same three cases
    ``FilesGrantSource`` falls back on, resolved here before the shares are read
    so the fallback still costs one statement rather than one per node.
    """
    wanted: set[uuid.UUID] = set()
    from_chain: set[uuid.UUID] = set()
    for node_id, node in page.nodes.items():
        if node.state != ACL_REWRITING and node.acl_id is not None:
            wanted.add(node.acl_id)
        else:
            from_chain.add(node_id)

    # A node answering from the chain inherits its ancestors' drive-default
    # grants, which live only in those ancestors' interned bodies — the home's
    # `{user: owner}`, `/Shared`'s `{org: reader}`, a team folder's team grant,
    # none of which has a `file_shares` row. Fold the ancestor bodies into THIS
    # statement (a superset of every anchor whose page role turns out to need
    # the chain, including one whose own `acl_id` dangles) so the whole page
    # still costs four statements, never a fifth per ancestor.
    for anchor_id in page.nodes:
        for ancestor in page.chains.get(anchor_id, ()):
            if ancestor.state != ACL_REWRITING and ancestor.acl_id is not None:
                wanted.add(ancestor.acl_id)

    acls: dict[uuid.UUID, FileAcl] = {}
    if wanted:
        stmt = repo.select_acls().where(FileAcl.id.in_(list(wanted)))
        acls = {row.id: row for row in (await repo.session.execute(stmt)).scalars().all()}

    for node_id, node in page.nodes.items():
        if node_id not in from_chain and node.acl_id not in acls:
            from_chain.add(node_id)
    return acls, from_chain


async def _load_shares(
    repo: FilesRepo, page: _Page, from_chain: set[uuid.UUID]
) -> dict[uuid.UUID, list[FileShare]]:
    """Every live grant on every chain that has to be read from the truth, in
    ONE statement, grouped by the node it sits on."""
    chain_ids = {ancestor.id for node_id in from_chain for ancestor in page.chains.get(node_id, ())}
    if not chain_ids:
        return {}
    stmt = repo.select_shares().where(
        FileShare.node_id.in_(list(chain_ids)),
        FileShare.revoked_at.is_(None),
    )
    grouped: dict[uuid.UUID, list[FileShare]] = {}
    for share in (await repo.session.execute(stmt)).scalars().all():
        grouped.setdefault(share.node_id, []).append(share)
    return grouped


def _grants_for(
    node: FileNode,
    chain: Sequence[FileNode],
    *,
    acls: Mapping[uuid.UUID, FileAcl],
    shares: Mapping[uuid.UUID, Sequence[FileShare]],
    from_chain: set[uuid.UUID],
) -> list[Grant]:
    """The node's grants, assembled from rows already in memory.

    Identical in meaning to ``FilesGrantSource.grants_for``: the interned body
    when the cache is trustworthy, otherwise the chain's own rows with the
    origin naming the ancestor that granted.
    """
    if node.id not in from_chain and node.acl_id is not None:
        body = acls[node.acl_id].body
        from_cache = [ace_to_grant(ace, node_id=node.id) for ace in body if isinstance(ace, dict)]
        return [grant for grant in from_cache if grant is not None]

    grants: list[Grant] = []
    for ancestor in chain:
        origin = (
            GrantOrigin.direct() if ancestor.id == node.id else GrantOrigin.inherited(ancestor.id)
        )
        for share in shares.get(ancestor.id, ()):
            grants.append(
                Grant(
                    principal=Principal(kind=share.principal_kind, id=share.principal_id),
                    role=share.role,
                    origin=origin,
                    expires_at=share.expires_at,
                    conditions=share.conditions,
                )
            )
        # The ancestor's drive-default grants — its home/Shared/Teams default,
        # which has no `file_shares` row — read from the interned body already
        # loaded (element-for-element the single-node `_from_chain` path).
        if ancestor.state != ACL_REWRITING and ancestor.acl_id is not None:
            minted_on = acls.get(ancestor.acl_id)
            if minted_on is not None:
                for default in drive_default_grants(minted_on.body, node_id=ancestor.id):
                    grants.append(
                        Grant(
                            principal=default.principal,
                            role=default.role,
                            origin=origin,
                            expires_at=default.expires_at,
                            conditions=default.conditions,
                        )
                    )
    return grants


@dataclass(frozen=True, slots=True)
class DecidedNode:
    """One node, its root-first ancestor chain, and this caller's access to it.

    The three things every surface that renders a decided row needs next: the
    row itself, the folders above it (the wire item names its location and its
    path out of them), and the access that says which affordances to draw. They
    ride together because the batched loader read all three in the same
    statements — a surface that took only the access would go back for the node
    and the chain one row at a time, which is the loop this module exists to
    delete.
    """

    node: FileNode
    chain: Sequence[FileNode]
    access: EffectiveAccess
    #: The drive the node lives in, read in the same statement as the node:
    #: the policy decides over its kind, and the decision's resource names it.
    drive: FileDrive


async def decided_by_id(
    repo: FilesRepo,
    ctx: ActingContext,
    node_ids: Iterable[NodeId],
    *,
    facts: AccessFacts | None = None,
    decider: AccessDecider = DEFAULT_DECIDER,
) -> dict[uuid.UUID, DecidedNode]:
    """Each id decided WITH the rows the decision was made from, in a constant
    number of queries per :data:`~alkera_core.files.repo.ID_BATCH` ids.

    :func:`access_by_id` is this function's access column; a caller that also
    renders the row takes this one so the node and the chain come from the same
    read as the answer about them, rather than from a second, racier one.

    An id that does not exist, or that belongs to another org, is simply absent
    from the mapping — the same silence :func:`readable_ids` keeps, for the same
    reason.

    The caller's list is unbounded and a statement's bind parameters are not, so
    the ids are resolved a batch at a time: a chat's attachment list grows for as
    long as the conversation does, and spelled as one ``IN`` the read would stop
    answering at all once it passed the driver's ceiling.
    """
    wanted = list(dict.fromkeys(node_ids))
    if not wanted:
        return {}
    resolved = facts if facts is not None else AccessFacts()

    decided: dict[uuid.UUID, DecidedNode] = {}
    for batch in id_batches(wanted):
        page = await _load_page(repo, batch)
        acls, from_chain = await _load_acls(repo, page)
        shares = await _load_shares(repo, page, from_chain)

        for node_id in batch:
            node = page.nodes.get(node_id)
            if node is None:
                continue
            chain = page.chains[node_id]
            grants = _grants_for(node, chain, acls=acls, shares=shares, from_chain=from_chain)
            drive = page.drives[node_id]
            decided[node_id] = DecidedNode(
                node=node,
                chain=chain,
                access=effective_role(ctx, node, chain, grants, drive, resolved, decider=decider),
                drive=drive,
            )
    return decided


async def access_by_id(
    repo: FilesRepo,
    ctx: ActingContext,
    node_ids: Iterable[NodeId],
    *,
    facts: AccessFacts | None = None,
    decider: AccessDecider = DEFAULT_DECIDER,
) -> dict[uuid.UUID, EffectiveAccess]:
    """Each id's OWN :class:`EffectiveAccess`, in a constant number of queries
    per :data:`~alkera_core.files.repo.ID_BATCH` ids.

    The decider reads a node's own ``flags`` and never its chain's, so an
    access resolved for one node says nothing true about another: a
    ``NO_DOWNLOAD`` chat folder sitting in a downloadable home, and the
    ``ARTIFACT`` outputs folder inside that chat, differ from their parent in
    exactly the capability the client gates its Download affordance on. A page
    that rendered every row with the access of the folder it listed therefore
    told the client the opposite of what the content route would answer — which
    is what this seam exists to stop, without paying a statement per row.

    An id that does not exist, or that belongs to another org, is simply absent
    from the mapping — the same silence :func:`readable_ids` keeps, for the same
    reason.
    """
    decided = await decided_by_id(repo, ctx, node_ids, facts=facts, decider=decider)
    return {node_id: row.access for node_id, row in decided.items()}


async def readable_ids(
    repo: FilesRepo,
    ctx: ActingContext,
    node_ids: Iterable[NodeId],
    *,
    facts: AccessFacts | None = None,
    action: FilesAction = FilesAction.READ,
    decider: AccessDecider = DEFAULT_DECIDER,
) -> set[uuid.UUID]:
    """The subset of ``node_ids`` this caller may ``action``.

    Equal, element for element, to resolving each id on its own through
    ``repo.chain`` + ``FilesGrantSource`` + ``effective_role`` — and four
    statements for the whole page rather than three per row. An id that does not
    exist, or that belongs to another org, is simply absent from the result: the
    caller's next step is a tombstone or a 404, and either way it must not be
    able to tell those two apart.

    The ``files.access`` policy for :data:`FilesAction.READ` is exactly
    "in the org and READ among the allowed actions", which is what
    :meth:`EffectiveAccess.allows` answers here — so a feed that filters with
    this seam refuses precisely what a per-item ``enforce()`` would have.
    """
    decided = await access_by_id(repo, ctx, node_ids, facts=facts, decider=decider)
    return {node_id for node_id, access in decided.items() if access.allows(action)}


__all__ = ["DecidedNode", "access_by_id", "decided_by_id", "readable_ids"]
