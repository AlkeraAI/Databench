"""In-memory reference models of the Files contract.

These model what the spec *says*, not what the implementation does: a namespace
is a dict of nodes keyed by an opaque handle, a quota is three numbers. Neither
model imports a service, issues SQL or looks at a row, so a divergence found by
a stateful test is a divergence from the intended semantics rather than from the
previous implementation. The one thing they do borrow is
:func:`alkera_core.files.conflicts.conflict_rename` — the conflict-name ladder is
part of the naming contract itself, and a second copy of it here would pin a
spelling rather than a behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from alkera_core.files.conflicts import conflict_rename

#: Kinds that may hold children, per the Layer 3 namespace contract.
CONTAINERS = frozenset({"folder"})


class ModelRefusal(Exception):  # noqa: N818 - it mirrors the Files error names, which drop the suffix
    """A refusal the contract requires, carrying the code the API must use."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ModelNode:
    """One node as the contract sees it: where it sits, its name, its version."""

    key: str
    kind: str
    name: bytes
    parent: str | None
    etag: int
    trash_op: str | None = None


@dataclass(slots=True)
class NamespaceModel:
    """The tree as a dict of handles, with the structural semantics of Layer 3."""

    root: str = "root"
    nodes: dict[str, ModelNode] = field(default_factory=dict)
    _next: int = 0

    def __post_init__(self) -> None:
        if self.root not in self.nodes:
            self.nodes[self.root] = ModelNode(self.root, "folder", b"", None, 1)

    # ---- reads -----------------------------------------------------------

    def live(self) -> dict[str, ModelNode]:
        """Every node the tree still shows — trashed subtrees are hidden."""
        return {key: node for key, node in self.nodes.items() if node.trash_op is None}

    def children(self, parent: str) -> dict[str, ModelNode]:
        return {key: node for key, node in self.live().items() if node.parent == parent}

    def taken(self, parent: str, *, excluding: str | None = None) -> set[bytes]:
        return {node.name for key, node in self.children(parent).items() if key != excluding}

    def descendants(self, key: str) -> set[str]:
        """``key`` and everything beneath it, trashed rows included."""
        found = {key}
        changed = True
        while changed:
            changed = False
            for handle, node in self.nodes.items():
                if node.parent in found and handle not in found:
                    found.add(handle)
                    changed = True
        return found

    # ---- writes ----------------------------------------------------------

    def create(self, parent: str, kind: str, name: bytes, *, conflict: str = "fail") -> str:
        node = self.nodes.get(parent)
        if node is None or node.trash_op is not None:
            raise ModelRefusal("files.not_found")
        if node.kind not in CONTAINERS:
            raise ModelRefusal("files.invalid_request")
        taken = self.taken(parent)
        if name in taken:
            if conflict != "rename":
                raise ModelRefusal("files.exists")
            name = conflict_rename(name, taken.__contains__)
        self._next += 1
        key = f"n{self._next}"
        self.nodes[key] = ModelNode(key, kind, name, parent, 1)
        return key

    def rename(self, key: str, name: bytes, *, if_match: int, conflict: str = "fail") -> bytes:
        node = self._live(key)
        if node.parent is None:
            raise ModelRefusal("files.invalid_request")
        taken = self.taken(node.parent, excluding=key)
        if name in taken and conflict == "rename":
            name = conflict_rename(name, taken.__contains__)
        # The precondition rides inside the write, so a stale etag is refused
        # whether or not the name it asked for was free.
        if node.etag != if_match:
            raise ModelRefusal("files.precondition_failed")
        if name in taken:
            raise ModelRefusal("files.exists")
        self.nodes[key] = replace(node, name=name, etag=node.etag + 1)
        return name

    def move(self, key: str, parent: str, *, if_match: int, conflict: str = "fail") -> None:
        node = self._live(key)
        target = self.nodes.get(parent)
        if target is None or target.trash_op is not None:
            raise ModelRefusal("files.not_found")
        if target.kind not in CONTAINERS:
            raise ModelRefusal("files.invalid_request")
        taken = self.taken(parent, excluding=key)
        name = node.name
        etag = node.etag
        if name in taken and conflict == "rename":
            # The conflict rename is its own write, so it consumes the etag and
            # the move that follows carries the bumped one. It is computed here
            # but not stored: the real move runs both writes in one transaction,
            # so a cycle refused after the rename rolls the rename back too and
            # a model that had already stored it would report a phantom etag.
            if etag != if_match:
                raise ModelRefusal("files.precondition_failed")
            # That rename lands while the node is still in the folder it is
            # leaving, so the name it takes has to be free in both folders —
            # a candidate free only at the destination would collide where the
            # node is standing.
            here = self.taken(node.parent, excluding=key) if node.parent is not None else set()
            name = conflict_rename(name, (taken | here).__contains__)
            etag += 1
            if_match += 1
        if etag != if_match:
            raise ModelRefusal("files.precondition_failed")
        if parent == key or parent in self.descendants(key):
            raise ModelRefusal("files.cycle")
        if name in taken:
            raise ModelRefusal("files.exists")
        self.nodes[key] = replace(node, parent=parent, name=name, etag=etag + 1)

    def trash(self, key: str, *, if_match: int) -> str:
        node = self._live(key)
        if node.parent is None:
            raise ModelRefusal("files.invalid_request")
        if node.etag != if_match:
            raise ModelRefusal("files.precondition_failed")
        self._next += 1
        op = f"t{self._next}"
        for handle in self.descendants(key):
            current = self.nodes[handle]
            if current.trash_op is None:
                self.nodes[handle] = replace(current, trash_op=op)
        return op

    def restore(self, op: str) -> bytes:
        """Un-trash exactly the subtree that op stamped, conflict-renaming its root."""
        stamped = [key for key, node in self.nodes.items() if node.trash_op == op]
        if not stamped:
            raise ModelRefusal("files.invalid_request")
        root = next(key for key in stamped if self.nodes[key].parent not in stamped)
        node = self.nodes[root]
        assert node.parent is not None
        parent: str = node.parent
        # A trashed ancestor cannot hold the restored root: it goes to the
        # nearest live ancestor, the same walk the service's restore performs.
        while self.nodes[parent].trash_op is not None:
            grandparent = self.nodes[parent].parent
            assert grandparent is not None
            parent = grandparent
        name = conflict_rename(node.name, self.taken(parent).__contains__)
        for handle in stamped:
            self.nodes[handle] = replace(self.nodes[handle], trash_op=None)
        self.nodes[root] = replace(self.nodes[root], name=name, parent=parent)
        return name

    def resync_etags(self, real: dict[str, int]) -> None:
        """Adopt the real etags after a whole-subtree write.

        Trash and restore stamp every node in the subtree; how many bumps each
        one costs is the trash layer's contract, not the namespace's, so this
        model pins their *structure* (names, parents, trashed state) and takes
        their version arithmetic from the rows. Every single-node write —
        create, rename, move — still asserts its own etag independently.
        """
        for key, etag in real.items():
            self.nodes[key] = replace(self.nodes[key], etag=etag)

    def _live(self, key: str) -> ModelNode:
        node = self.nodes.get(key)
        if node is None or node.trash_op is not None:
            raise ModelRefusal("files.not_found")
        return node


@dataclass(slots=True)
class QuotaModel:
    """Committed usage, the open holds, and the two ceilings."""

    quota_bytes: int
    quota_nodes: int
    used_bytes: int = 0
    used_nodes: int = 0
    holds: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def held_bytes(self) -> int:
        return sum(hold[0] for hold in self.holds.values())

    @property
    def held_nodes(self) -> int:
        return sum(hold[1] for hold in self.holds.values())

    def _refuses_bytes(self, *, size: int, nodes: int) -> bool:
        """Whether the byte ceiling refuses this reserve.

        Safety mode: a reserve that *adds* anything — bytes or nodes — is
        refused once the drive's bytes are AT the ceiling, not only past it, so
        a full drive creates nothing new until it drops below again. A reserve
        that adds nothing is never refused by that rule, which is what lets a
        full drive still be read, swept and tidied. The node ceiling has no
        such rule: it refuses only what would land past it.
        """
        spent = self.used_bytes + self.held_bytes
        adds = size > 0 or nodes > 0
        return (adds and spent >= self.quota_bytes) or spent + size > self.quota_bytes

    def fits(self, *, size: int, nodes: int) -> bool:
        return not self._refuses_bytes(size=size, nodes=nodes) and (
            self.used_nodes + self.held_nodes + nodes <= self.quota_nodes
        )

    def reserve(self, session: str, *, size: int, nodes: int) -> None:
        if size < 0 or nodes < 0:
            raise ModelRefusal("files.quota_bytes" if size < 0 else "files.quota_nodes")
        if self._refuses_bytes(size=size, nodes=nodes):
            raise ModelRefusal("files.quota_bytes")
        if self.used_nodes + self.held_nodes + nodes > self.quota_nodes:
            raise ModelRefusal("files.quota_nodes")
        self.holds[session] = (size, nodes)

    def release(self, session: str) -> None:
        self.holds.pop(session, None)

    def reconcile(self, session: str, *, actual_bytes: int, actual_nodes: int) -> None:
        self.holds.pop(session, None)
        self.used_bytes += actual_bytes
        self.used_nodes += actual_nodes

    def expire(self, sessions: set[str]) -> int:
        """Free every named hold; the count is what the sweeper must report."""
        freed = 0
        for session in sessions:
            if session in self.holds:
                del self.holds[session]
                freed += 1
        return freed


#: The ladder as the permission model documents it, weakest rung first. A second
#: copy on purpose: the model must be able to disagree with the table the code
#: reads, or it could never catch a rung silently reordered there.
ROLE_ORDER: tuple[str, ...] = ("reader", "commenter", "writer", "manager", "owner")


@dataclass(frozen=True, slots=True)
class ModelGrant:
    """One share row as the contract sees it."""

    node: str
    principal: str
    role: str


@dataclass(slots=True)
class AclModel:
    """Grants per node; a role is the strongest rung the ancestor chain gives.

    There is no cache here and no ``acl_id``: the union over the chain *is* the
    permission set, which is what makes this model able to say whether the
    implementation's cached body drifted from the truth it copies.
    """

    #: node -> parent, the tree the chain walk follows.
    parents: dict[str, str | None] = field(default_factory=dict)
    #: share handle -> the grant it holds. Revoked shares are dropped.
    grants: dict[str, ModelGrant] = field(default_factory=dict)
    #: The drive's defaults, which every node inherits from above the root.
    defaults: dict[str, str] = field(default_factory=dict)
    #: How many shares have ever been minted. Handles come from this and never
    #: from how many are *live*: a revoke frees a name, and reusing it would
    #: silently re-point an older grant at a newer share's node, which reads as
    #: an implementation divergence when the model is the thing that moved.
    minted: int = 0

    def chain(self, node: str) -> list[str]:
        """Root first, ``node`` last — the ancestors a grant descends through."""
        walk: list[str] = []
        cursor: str | None = node
        while cursor is not None:
            walk.append(cursor)
            cursor = self.parents[cursor]
        return list(reversed(walk))

    def descendants(self, node: str) -> set[str]:
        found = {node}
        changed = True
        while changed:
            changed = False
            for key, parent in self.parents.items():
                if parent in found and key not in found:
                    found.add(key)
                    changed = True
        return found

    def union(self, node: str) -> set[tuple[str, str, str]]:
        """``(principal, role, origin)`` for the whole chain plus the defaults.

        The origin is a fact about *where* the grant sits relative to ``node``,
        so it is derived here rather than carried on the grant: the same share
        row is ``direct`` for its own node and ``inherited`` for every node
        below it.
        """
        aces: set[tuple[str, str, str]] = {
            (principal, role, "drive_default") for principal, role in self.defaults.items()
        }
        for ancestor in self.chain(node):
            for grant_ in self.grants.values():
                if grant_.node == ancestor:
                    origin = "direct" if ancestor == node else "inherited"
                    aces.add((grant_.principal, grant_.role, origin))
        return aces

    def effective_role(self, principal: str, node: str) -> str | None:
        """The strongest rung the chain gives ``principal`` on ``node``."""
        best: str | None = None
        for granted_principal, role, _origin in self.union(node):
            if granted_principal != principal or role not in ROLE_ORDER:
                continue
            if best is None or ROLE_ORDER.index(role) > ROLE_ORDER.index(best):
                best = role
        return best

    def standing(self, node: str, principal: str) -> str | None:
        """The handle of the live share ``principal`` holds on ``node``, if any."""
        for handle, held in self.grants.items():
            if held.node == node and held.principal == principal:
                return handle
        return None

    def grant(self, node: str, principal: str, role: str) -> str:
        """Record a share and return the handle it is known by from now on.

        A principal has one access to one node, so granting where they already
        hold a live share moves that share to the new rung and keeps its
        handle: a second row would say two things at once, and since a body
        takes the strongest of them the weaker one would be invisible right up
        until somebody withdrew the other and the access stayed.

        Where there is no standing share the model mints the handle rather than
        taking one, because the handle *is* the share's identity here and only
        the model knows which names it has already spent.
        """
        if role not in ROLE_ORDER:
            raise ModelRefusal("files.invalid_request")
        held = self.standing(node, principal)
        if held is not None:
            self.grants[held] = replace(self.grants[held], role=role)
            return held
        self.minted += 1
        handle = f"s{self.minted}"
        self.grants[handle] = ModelGrant(node=node, principal=principal, role=role)
        return handle

    def revoke(self, share: str, node: str) -> None:
        """Withdraw a grant that lives on ``node``.

        A grant the node merely inherits is refused rather than shadowed: there
        are no deny entries, so the only honest way to remove it is at its own
        node, and the refusal names that node.
        """
        held = self.grants.get(share)
        if held is None:
            raise ModelRefusal("files.not_found")
        if held.node != node:
            if held.node in self.chain(node):
                raise ModelRefusal("files.inherited_grant")
            raise ModelRefusal("files.not_found")
        del self.grants[share]

    def move(self, node: str, parent: str) -> None:
        """Re-hang a subtree; every inherited role below it re-derives by chain."""
        if parent == node or parent in self.descendants(node):
            raise ModelRefusal("files.cycle")
        self.parents[node] = parent


@dataclass(frozen=True, slots=True)
class ModelLease:
    """The one holder a node may have, at the one epoch it was handed."""

    epoch: int
    instance: str
    principal: str
    #: False once the deadline has passed or the holder handed it back.
    live: bool
    #: Forced leases are in their grace period: still live, no longer extended.
    forced: bool = False
    #: The deadline passed recently enough that the grant delay still stands:
    #: the folder is nobody else's to take, but it is no longer live either.
    #: A hand-back clears it — a holder that let go is not owed the delay.
    lapsed: bool = False


@dataclass(slots=True)
class LeaseModel:
    """One holder + epoch per node, fenced writes, no overlapping mounts."""

    parents: dict[str, str | None] = field(default_factory=dict)
    leases: dict[str, ModelLease] = field(default_factory=dict)
    #: The high-water mark that outlives a lease row, so an epoch is never reused.
    hwm: dict[str, int] = field(default_factory=dict)

    def chain(self, node: str) -> list[str]:
        walk: list[str] = []
        cursor: str | None = node
        while cursor is not None:
            walk.append(cursor)
            cursor = self.parents[cursor]
        return list(reversed(walk))

    def descendants(self, node: str) -> set[str]:
        found = {node}
        changed = True
        while changed:
            changed = False
            for key, parent in self.parents.items():
                if parent in found and key not in found:
                    found.add(key)
                    changed = True
        return found

    def live_leases(self) -> dict[str, ModelLease]:
        return {node: lease for node, lease in self.leases.items() if lease.live}

    def covering(self, node: str) -> tuple[str, ModelLease] | None:
        """The live lease on ``node`` or on its nearest leased ancestor."""
        for ancestor in reversed(self.chain(node)):
            lease = self.leases.get(ancestor)
            if lease is not None and lease.live:
                return ancestor, lease
        return None

    def acquire(self, node: str, instance: str, principal: str) -> tuple[int, bool]:
        """Take the lease, refusing every overlap; returns the epoch and whether it resumed.

        The same principal asking again from the same instance is the mount that
        was killed and has come back: it takes its own lease over instead of
        waiting out a TTL nobody is using, and it *keeps its epoch*, because
        nothing was ever fenced — the writes it had in flight are still its own.
        A forced lease is the exception: a force pushes the moment the folder
        becomes grantable into the future, so during the grace period nobody
        takes it, the holder included.

        Once the deadline has actually passed the folder is still not up for
        grabs: for one grant delay it stays the lapsed holder's, so a machine
        whose beat was merely slow is not raced off a folder it is still
        writing. Its own instance is exempt — that exemption is what makes the
        delay protect the holder rather than lock it out — but it comes back at
        a NEW epoch, because everything it had in flight was fenced the moment
        the lease lapsed. Any other asker, the same person on a second
        instance included, is refused until the delay runs out.

        The overlap check runs first, exactly as the service runs it before its
        one statement, so a resume under a live descendant lease is still
        refused rather than silently granted.
        """
        depth = len(self.chain(node))
        for other, lease in self.live_leases().items():
            if other == node:
                continue
            if other not in self.descendants(node) and node not in self.descendants(other):
                continue
            # A lease strictly above one the same principal already holds is
            # that principal's own mount, not a second writer on one folder.
            if len(self.chain(other)) < depth and lease.principal == principal:
                continue
            raise ModelRefusal("files.leased")
        held = self.leases.get(node)
        own = held is not None and held.instance == instance and held.principal == principal
        if held is not None and held.live:
            if held.forced or not own:
                raise ModelRefusal("files.leased")
            return held.epoch, True
        if held is not None and held.lapsed and not own:
            raise ModelRefusal("files.leased")
        epoch = max(held.epoch if held is not None else 0, self.hwm.get(node, 0)) + 1
        self.leases[node] = ModelLease(
            epoch=epoch, instance=instance, principal=principal, live=True
        )
        self.hwm[node] = max(self.hwm.get(node, 0), epoch)
        return epoch, False

    def _holding(
        self, node: str, epoch: int, instance: str, caller: str | None = None
    ) -> ModelLease:
        lease = self.leases.get(node)
        if lease is None or not lease.live or lease.epoch != epoch or lease.instance != instance:
            raise ModelRefusal("files.lease_fenced")
        if caller is not None and caller != lease.principal:
            # The same third half a write compares. Keeping a lease alive and
            # handing it back are the holder's own verbs: the route decides
            # them on a rung a plain writer holds, and the pair they name is
            # derivable from what the product serves, so a caller who is not
            # the holder must be refused exactly as a superseded one is.
            raise ModelRefusal("files.lease_fenced")
        return lease

    def heartbeat(self, node: str, epoch: int, instance: str, caller: str | None = None) -> bool:
        """Extend the lease; returns whether the holder was told it is forced."""
        return self._holding(node, epoch, instance, caller).forced

    def release(self, node: str, epoch: int, instance: str, caller: str | None = None) -> None:
        """Hand the folder back: it is the next asker's immediately.

        The grant delay exists to protect a holder that did not mean to let go;
        one that released said it is done, so no delay is owed.
        """
        lease = self._holding(node, epoch, instance, caller)
        self.leases[node] = replace(lease, live=False, forced=False, lapsed=False)

    def force(self, node: str) -> None:
        lease = self.leases.get(node)
        if lease is None or not lease.live:
            raise ModelRefusal("files.not_found")
        self.leases[node] = replace(lease, forced=True)

    def expire(self, node: str, *, past_delay: bool = False) -> None:
        """The deadline passes and the holder loses the folder.

        ``past_delay`` says how long ago: a lease that lapsed a moment ago is
        still nobody else's while the grant delay stands, and one that lapsed
        long enough ago is the next asker's. Either way the holder's epoch is
        spent — it comes back, if it comes back, above the one it was fenced at.
        """
        lease = self.leases.get(node)
        if lease is not None:
            self.leases[node] = replace(lease, live=False, forced=False, lapsed=not past_delay)

    def write(
        self, node: str, epoch: int | None, instance: str | None, writer: str | None = None
    ) -> None:
        """The fence every write runs: land, or name why it may not.

        A mount refuses every writer but its holder. ``writer`` is the third
        thing the fence compares and the one a request cannot fake: the epoch
        and the instance are both derivable from what the product serves, so a
        colleague who replays the holder's pair names the lease correctly and
        is still not the holder. ``None`` means the case does not vary the
        writer and the holder is writing.

        The one lease that refuses nobody — a chat's, which takes what a person
        drops into the working directory while the box runs — is admitted by
        purpose and by where the write lands, neither of which is a fact about
        the tree this model holds; those cases are pinned directly against the
        service.
        """
        covering = self.covering(node)
        if covering is None:
            return
        if epoch is None or instance is None:
            raise ModelRefusal("files.leased")
        _holder_node, lease = covering
        if lease.epoch != epoch or lease.instance != instance:
            raise ModelRefusal("files.lease_fenced")
        if writer is not None and writer != lease.principal:
            raise ModelRefusal("files.lease_fenced")
