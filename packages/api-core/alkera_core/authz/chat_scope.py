"""Who a document is addressed to, spelled once for both surfaces.

The REST policy and the socket's channel rules must agree by construction, so
there is one predicate with two callers. :func:`chat_readable` is that
predicate for a chat: a
document with no team is the whole org's, a document narrowed to a team is that
team's and the org admins', and nothing else reads it. The socket's
``doc_readable`` applies it to the row it locked; the ``CHAT`` policy applies it
to the facts a route resolved. The agreement is tested over a matrix of admins,
members and team scopes, so a change to one caller that drifts from the other
fails.

:func:`scope_readable` is the same question for a workspace object, whose
audience is written as a scope string rather than a team column: ``org`` is
every member, ``team:<uuid>`` defers to :func:`chat_readable`, ``private`` is
the owner (and the org admins) alone, and an unrecognised scope reads to
nobody.

A chat document also has a *rung*: a chat is a node in the file tree, and a
member who was shared that node at ``writer`` may do more with the live
document than a member the chat's audience merely lets watch.
:func:`chat_doc_role` is that answer (the publisher's rung, or the rung the
node's ACL gives the caller), and the ladder in
:mod:`alkera_core.files.authz.ladder` decides what a rung allows. The ACL is
authoritative for the shared rung *because it is the only place a grant can be
made*: the chat's ``visibility_scope`` says who may watch, the node's ACL says
what each of them may do, and a rung is never derived from the scope.

All of them are pure: sets, ids and a resolved rung in, an answer out, no
database and no request. Resolving the rung is the caller's job, because that
is a query; deciding what it means is this module's.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from uuid import UUID

#: A workspace object visible to the whole org.
SCOPE_ORG = "org"
#: A workspace object visible only to its owner.
SCOPE_PRIVATE = "private"
#: The prefix of a scope that names one team.
SCOPE_TEAM_PREFIX = "team:"

#: The rung a chat's publisher holds on its own document — the top of the role
#: ladder, spelled here rather than imported so that this module stays free of
#: the Files package (``alkera_core.files`` depends on ``alkera_core.authz``,
#: not the other way round). That the two spellings agree is pinned by a test,
#: so renaming the ladder's rung fails loudly instead of silently demoting
#: every publisher.
PUBLISHER_ROLE = "owner"

#: The rung a member needs before they may drive a chat's agent, and the rungs
#: that clear it. Spelled here for the same reason as :data:`PUBLISHER_ROLE` —
#: this module stays free of the Files package — and pinned against the ladder
#: by a test, so moving ``WRITE`` between rungs fails loudly instead of
#: silently letting a commenter speak for the conversation.
SEND_ROLE = "writer"
SEND_ROLES = frozenset({SEND_ROLE, "manager", PUBLISHER_ROLE})

#: The rungs that may change what a chat is CALLED. A chat is a node in the
#: file tree and renaming a node is the Files ladder's ``WRITE``, which starts
#: at "Can edit" — so the rungs are the ladder's write rungs, spelled here for
#: the same reason as :data:`SEND_ROLES` and pinned against the ladder by a
#: test. Kept separate from :data:`SEND_ROLES` because the two answer different
#: questions: one is "may you drive the agent", the other "may you retitle the
#: conversation", and a later product decision may move either alone.
RENAME_ROLES = frozenset({SEND_ROLE, "manager", PUBLISHER_ROLE})

#: Every rung the ladder knows. A chat is private until it is shared, and a
#: share is a rung on the chat's node: holding ANY of these — "Can view" and
#: up, whether granted on the node itself, inherited from a folder the node
#: sits in, or made to a team the caller is on — is what admits a reader who
#: is not the chat's owner. Spelled here for the same reason as
#: :data:`SEND_ROLES` and pinned against the ladder by a test.
READ_ROLES = frozenset({"reader", "commenter", SEND_ROLE, "manager", PUBLISHER_ROLE})


def role_may_read(role: str | None) -> bool:
    """Whether a rung on the chat's node lets its holder read the chat.

    Any rung the ladder knows does — the weakest rung is "Can view". ``None``,
    the empty string (no grant reaches this caller) and a rung the ladder has
    never heard of admit nobody: an unknown rung is not a weak rung.
    """
    return role is not None and role in READ_ROLES


def chat_read_reason(
    *,
    is_owner: bool,
    is_bound_machine: bool,
    shared_role: str | None,
    is_org_admin: bool,
    admin_reads_private: bool,
) -> str | None:
    """WHY a member of the chat's org may read the chat, or ``None``.

    The one answer every door onto a chat is built from — the REST policy (which
    files the reason on the decision row), the objects listing, the chat rail and
    the socket's channel rules. It is a *reason* rather than a bool so the policy
    does not need a second, parallel spelling of the same four cases, which is
    how a door comes to answer differently from the door beside it.

    Its owner; the machine the chat is bound to, speaking as itself (the box
    reads the transcript it publishes); anyone holding a rung on the chat's
    node; and last, an org admin, but only where the deployment says so. A chat
    with no node (created before the drive existed, or with Files off) has no
    rungs, so it is the owner's alone. Tenancy and membership are decided by the
    caller first, as with every predicate here.

    ``admin_reads_private`` is deliberately a required argument with no default:
    a door that has not thought about it cannot call this at all.
    """
    if is_owner:
        return "owner_reads"
    if is_bound_machine:
        return "bound_machine_reads"
    if role_may_read(shared_role):
        return "shared_reads"
    if admin_reads_private and is_org_admin:
        return "org_admin_reads_private"
    return None


def chat_visible(
    *,
    is_owner: bool,
    is_bound_machine: bool,
    shared_role: str | None,
    is_org_admin: bool,
    admin_reads_private: bool,
) -> bool:
    """Whether a member of the chat's org may read the chat at all —
    :func:`chat_read_reason` without the reason."""
    return (
        chat_read_reason(
            is_owner=is_owner,
            is_bound_machine=is_bound_machine,
            shared_role=shared_role,
            is_org_admin=is_org_admin,
            admin_reads_private=admin_reads_private,
        )
        is not None
    )


def role_may_send(role: str | None) -> bool:
    """Whether a rung on the chat's node lets its holder send to the agent.

    Sending is not a read. The reader never touches the transcript, but the
    answer their prompt produces lands in the document everybody in the
    audience is watching and is billed to the chat's owner — so driving the
    agent is the ladder's WRITE, not its READ. ``None`` and any rung this
    ladder has never heard of allow nothing.
    """
    return role is not None and role in SEND_ROLES


def role_may_rename(role: str | None) -> bool:
    """Whether a rung on the chat's node lets its holder retitle the chat.

    A chat's title is the name of its folder, and naming a node is the Files
    ladder's WRITE — so "Can edit" and up rename, and "Can view" and "Can
    comment" do not. ``None`` and a rung this ladder has never heard of allow
    nothing.
    """
    return role is not None and role in RENAME_ROLES


def team_of_scope(scope: str) -> UUID | None:
    """The team a ``team:<uuid>`` scope names; ``None`` for any other scope,
    including a malformed one — the caller decides what an unparseable scope
    means, and every caller here decides against the reader."""
    if not scope.startswith(SCOPE_TEAM_PREFIX):
        return None
    try:
        return UUID(scope.removeprefix(SCOPE_TEAM_PREFIX))
    except ValueError:
        return None


def scope_for_team(team_id: UUID | None) -> str:
    """The scope string that addresses ``team_id``'s audience: ``org`` for no
    team, ``team:<uuid>`` otherwise. The one spelling every writer of a
    workspace object's ``visibility_scope`` uses, so that what was written is
    what the predicates below can parse."""
    return SCOPE_ORG if team_id is None else f"{SCOPE_TEAM_PREFIX}{team_id}"


def require_scope_team(visibility_scope: str, team_id: UUID | None) -> bool:
    """Whether a visibility scope and a team column name the same audience.

    They are two spellings of one fact, resolved from one row, so disagreement
    is a bug in the writer rather than a case to interpret, and every reader —
    the REST policy and the socket alike — refuses the row. An unrecognised
    scope never agrees with anything.
    """
    if visibility_scope in (SCOPE_ORG, SCOPE_PRIVATE):
        return team_id is None
    scope_team = team_of_scope(visibility_scope)
    return scope_team is not None and scope_team == team_id


def chat_readable(
    *, is_org_admin: bool, team_ids: AbstractSet[UUID], doc_team_id: UUID | None
) -> bool:
    """Whether a member of the document's org may read it.

    ``team_ids`` is the reader's materialized membership chain (a member of a
    sub-team holds a row for every ancestor), so a document narrowed to an
    ancestor team is readable by the members below it. Tenancy is NOT decided
    here: the caller has already established that reader and document share an
    org.
    """
    return doc_team_id is None or is_org_admin or doc_team_id in team_ids


def scope_readable(
    *,
    is_org_admin: bool,
    team_ids: AbstractSet[UUID],
    visibility_scope: str,
    is_owner: bool,
) -> bool:
    """Whether a member of the object's org may read an object at
    ``visibility_scope``. An org admin reads every scope the org holds; every
    other reader is admitted by the scope alone, and an owner reads their own
    private object.
    """
    if is_org_admin:
        return True
    if visibility_scope == SCOPE_PRIVATE:
        return is_owner
    if visibility_scope == SCOPE_ORG:
        return True
    team_id = team_of_scope(visibility_scope)
    if team_id is None:
        return False
    return chat_readable(is_org_admin=is_org_admin, team_ids=team_ids, doc_team_id=team_id)


def chat_doc_role(
    *,
    owner_user_id: UUID | None,
    user_id: UUID | None,
    bound_machine_id: str | None,
    agent_id: str | None,
    shared_role: str | None = None,
) -> str | None:
    """The rung a reader of a chat holds on its live document, or ``None``.

    Two publishers exist and no other: the chat's owner, and the machine the
    chat is bound to, speaking as an agent whose asserted id IS that machine's
    id. The second is what lets one box serve every member's chat: the chat
    row names the machine that runs it, the daemon on that machine asserts the
    id it registered as, and the two must be the same string. A chat bound to
    no machine has no machine publisher; an agent whose id names some other
    machine (or nothing) is judged by ``shared_role`` like anyone else.

    ``shared_role`` is the rung the chat's node in the file tree gives this
    caller, resolved by the caller (it is a query, and this module does not do
    queries). ``None`` — no node, no grant, or a cache the ACL rewrite has not
    repaired yet — is not an error: it is a reader, which is what everyone the
    audience admits already is.

    A publisher outranks its grant, never the other way round: this returns
    :data:`PUBLISHER_ROLE` for the two publishers and ``shared_role`` for
    everybody else, so a shared rung can widen what a reader may do and can
    never make a non-publisher into the document's publisher. Being the
    publisher is asked separately, by :func:`chat_writable`, precisely so that
    a deliberate grant of the top rung on the node still does not hand out the
    right to rewrite a transcript.

    Membership is NOT decided here, the same way tenancy is not decided by
    :func:`chat_readable`: every caller applies the read predicate first, so a
    rung is only ever held by a reader the audience already admits.
    """
    if chat_writable(
        owner_user_id=owner_user_id,
        user_id=user_id,
        bound_machine_id=bound_machine_id,
        agent_id=agent_id,
    ):
        return PUBLISHER_ROLE
    return shared_role


def chat_writable(
    *,
    owner_user_id: UUID | None,
    user_id: UUID | None,
    bound_machine_id: str | None,
    agent_id: str | None,
) -> bool:
    """Whether this caller is the chat document's PUBLISHER — the one identity
    that may append to the transcript, stream on the ephemeral lane, and
    replace the state wholesale.

    The chat's owner, or the machine the chat is bound to speaking as itself.
    Nothing else, and in particular no grant: a node shared at the top rung
    lets a colleague write the document's meta, never rewrite its history.
    ``user_id`` is ``None`` for a box on its own machine credential — nobody
    is behind it, so it is never the owner and only the binding can admit it.
    """
    if owner_user_id is not None and owner_user_id == user_id:
        return True
    return bool(bound_machine_id) and bool(agent_id) and agent_id == bound_machine_id


__all__ = [
    "PUBLISHER_ROLE",
    "READ_ROLES",
    "RENAME_ROLES",
    "SCOPE_ORG",
    "SCOPE_PRIVATE",
    "SCOPE_TEAM_PREFIX",
    "SEND_ROLE",
    "SEND_ROLES",
    "chat_doc_role",
    "chat_read_reason",
    "chat_readable",
    "chat_visible",
    "chat_writable",
    "require_scope_team",
    "role_may_read",
    "role_may_rename",
    "role_may_send",
    "scope_for_team",
    "scope_readable",
    "team_of_scope",
]
