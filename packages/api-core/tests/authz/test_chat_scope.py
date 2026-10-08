"""The one predicate both the REST policy and the socket's channel rules call.

Its whole reason to exist is that two surfaces cannot drift, so its own table
is exhaustive over the shape of the question: a document with no team, one
narrowed to a team the reader holds, one narrowed to a team they do not, and
the org admin who reads across all of them. :func:`scope_readable` adds the
scope grammar on top and is pinned the same way, including every spelling that
is not a scope we serve.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from alkera_core.authz.chat_scope import (
    PUBLISHER_ROLE,
    READ_ROLES,
    RENAME_ROLES,
    SCOPE_ORG,
    SCOPE_PRIVATE,
    SEND_ROLE,
    chat_doc_role,
    chat_read_reason,
    chat_readable,
    chat_visible,
    chat_writable,
    role_may_read,
    role_may_rename,
    role_may_send,
    scope_readable,
    team_of_scope,
)
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.ladder import (
    DEFAULT_LADDER,
    ROLE_COMMENTER,
    ROLE_OWNER,
    ROLE_READER,
    ROLE_WRITER,
)

TEAM = UUID("00000000-0000-4000-8000-00000000000b")
OTHER_TEAM = UUID("00000000-0000-4000-8000-00000000000c")


@pytest.mark.parametrize(
    ("is_org_admin", "team_ids", "doc_team_id", "expected"),
    [
        pytest.param(False, frozenset(), None, True, id="org-wide-doc-any-member"),
        pytest.param(True, frozenset(), None, True, id="org-wide-doc-org-admin"),
        pytest.param(False, frozenset({TEAM}), TEAM, True, id="team-doc-team-member"),
        pytest.param(
            False,
            frozenset({TEAM, OTHER_TEAM}),
            TEAM,
            True,
            id="team-doc-member-of-several-teams",
        ),
        pytest.param(False, frozenset({OTHER_TEAM}), TEAM, False, id="team-doc-another-team"),
        pytest.param(False, frozenset(), TEAM, False, id="team-doc-no-memberships"),
        pytest.param(True, frozenset(), TEAM, True, id="team-doc-org-admin"),
    ],
)
def test_chat_readable_table(
    is_org_admin: bool, team_ids: frozenset[UUID], doc_team_id: UUID | None, expected: bool
) -> None:
    assert (
        chat_readable(is_org_admin=is_org_admin, team_ids=team_ids, doc_team_id=doc_team_id)
        is expected
    )


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        pytest.param(f"team:{TEAM}", TEAM, id="team-scope"),
        pytest.param(SCOPE_ORG, None, id="org"),
        pytest.param(SCOPE_PRIVATE, None, id="private"),
        pytest.param("team:", None, id="empty-team-id"),
        pytest.param("team:not-a-uuid", None, id="unparseable-team-id"),
        pytest.param(f"TEAM:{TEAM}", None, id="prefix-is-case-sensitive"),
        pytest.param("", None, id="empty-scope"),
    ],
)
def test_team_of_scope_table(scope: str, expected: UUID | None) -> None:
    assert team_of_scope(scope) == expected


@pytest.mark.parametrize(
    ("scope", "is_org_admin", "team_ids", "is_owner", "expected"),
    [
        pytest.param(SCOPE_ORG, False, frozenset(), False, True, id="org-scope-any-member"),
        pytest.param(SCOPE_PRIVATE, False, frozenset(), True, True, id="private-owner"),
        pytest.param(SCOPE_PRIVATE, False, frozenset(), False, False, id="private-not-owner"),
        pytest.param(SCOPE_PRIVATE, True, frozenset(), False, True, id="private-org-admin"),
        pytest.param(f"team:{TEAM}", False, frozenset({TEAM}), False, True, id="team-member"),
        pytest.param(
            f"team:{TEAM}", False, frozenset({OTHER_TEAM}), False, False, id="team-outsider"
        ),
        pytest.param(f"team:{TEAM}", True, frozenset(), False, True, id="team-org-admin"),
        pytest.param(
            f"team:{TEAM}",
            False,
            frozenset({OTHER_TEAM}),
            True,
            False,
            id="owning-a-team-scoped-object-is-not-reading-it",
        ),
        pytest.param("team:nope", False, frozenset(), True, False, id="unparseable-team-scope"),
        pytest.param("world", False, frozenset(), True, False, id="unknown-scope-grammar"),
        pytest.param("", False, frozenset(), True, False, id="empty-scope"),
        pytest.param("world", True, frozenset(), False, True, id="unknown-scope-org-admin"),
    ],
)
def test_scope_readable_table(
    scope: str, is_org_admin: bool, team_ids: frozenset[UUID], is_owner: bool, expected: bool
) -> None:
    assert (
        scope_readable(
            is_org_admin=is_org_admin,
            team_ids=team_ids,
            visibility_scope=scope,
            is_owner=is_owner,
        )
        is expected
    )


USER = UUID("00000000-0000-4000-8000-0000000000a1")
OTHER_USER = UUID("00000000-0000-4000-8000-0000000000a2")
MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"


@pytest.mark.parametrize(
    ("owner_user_id", "user_id", "bound_machine_id", "agent_id", "expected"),
    [
        pytest.param(USER, USER, None, None, True, id="owner-on-their-own-socket"),
        pytest.param(USER, USER, MACHINE, None, True, id="owner-of-a-bound-chat"),
        pytest.param(USER, USER, MACHINE, "machine:other", True, id="owner-asserting-any-agent"),
        pytest.param(USER, OTHER_USER, None, None, False, id="reader-without-an-assertion"),
        pytest.param(USER, OTHER_USER, MACHINE, MACHINE, True, id="the-bound-machine-as-an-agent"),
        pytest.param(
            USER, OTHER_USER, MACHINE, "machine:other", False, id="another-machines-agent"
        ),
        pytest.param(USER, OTHER_USER, MACHINE, None, False, id="bound-chat-no-assertion"),
        pytest.param(USER, OTHER_USER, None, MACHINE, False, id="unbound-chat-any-agent"),
        pytest.param(USER, OTHER_USER, "", MACHINE, False, id="empty-binding-is-no-machine"),
        pytest.param(USER, OTHER_USER, MACHINE, "", False, id="empty-assertion-is-no-agent"),
        pytest.param(USER, OTHER_USER, MACHINE, MACHINE.upper(), False, id="ids-compare-exactly"),
        pytest.param(None, USER, None, None, False, id="unowned-unbound-nobody-writes"),
        pytest.param(None, USER, MACHINE, MACHINE, True, id="unowned-but-bound-machine-writes"),
    ],
)
def test_chat_writable_table(
    owner_user_id: UUID | None,
    user_id: UUID,
    bound_machine_id: str | None,
    agent_id: str | None,
    expected: bool,
) -> None:
    """Two publishers and no other: the owner, and the bound machine speaking
    as itself. Everything else — a reader, a stranger's agent id, a chat bound
    to nothing, an empty id on either side — is a reader."""
    assert (
        chat_writable(
            owner_user_id=owner_user_id,
            user_id=user_id,
            bound_machine_id=bound_machine_id,
            agent_id=agent_id,
        )
        is expected
    )


# ---------------------------------------------------------------------------
# The rung a reader holds on a chat's live document
# ---------------------------------------------------------------------------


def test_the_publishers_rung_is_the_ladders_top_rung() -> None:
    """``chat_scope`` spells the top rung itself rather than importing it, so
    that the authz package stays free of the Files package it sits underneath.
    That is only safe while the two spellings agree — renaming the ladder's
    rung without this constant would silently demote every publisher to a role
    the ladder allows nothing for, which is exactly the kind of failure that
    passes every other test.
    """
    assert PUBLISHER_ROLE == ROLE_OWNER
    assert DEFAULT_LADDER.rank(PUBLISHER_ROLE) == len(DEFAULT_LADDER.roles) - 1
    assert DEFAULT_LADDER.allows(PUBLISHER_ROLE, FilesAction.WRITE)


def test_the_send_rung_is_exactly_the_ladders_write_rungs() -> None:
    """``role_may_send`` is spelled here for the same reason the publisher's
    rung is, and is only safe while it agrees with the ladder. Moving ``WRITE``
    up or down a rung without this constant would quietly let a commenter drive
    somebody else's agent, or lock a writer out of their own shared chat —
    neither of which any other test would notice.
    """
    assert SEND_ROLE == ROLE_WRITER
    for role in DEFAULT_LADDER.roles:
        assert role_may_send(role) is DEFAULT_LADDER.allows(role, FilesAction.WRITE), role
    assert role_may_send(None) is False
    assert role_may_send("editor") is False


def test_the_read_rungs_are_exactly_the_ladders_read_rungs() -> None:
    """A chat is private until shared, and "shared" means any rung the ladder
    lets READ — which today is every rung, "Can view" being the weakest. The
    constant is spelled in the authz package for the same reason the send rung
    is, so it must agree with the ladder: a rung added below ``reader`` that
    did not read, or one the ladder dropped, would otherwise silently admit or
    lock out a reader with no other test noticing."""
    assert READ_ROLES == frozenset(DEFAULT_LADDER.roles)
    for role in DEFAULT_LADDER.roles:
        assert role_may_read(role) is DEFAULT_LADDER.allows(role, FilesAction.READ), role
    assert role_may_read(None) is False
    assert role_may_read("") is False
    assert role_may_read("editor") is False


@pytest.mark.parametrize(
    ("is_owner", "is_bound_machine", "shared_role", "visible"),
    [
        pytest.param(True, False, None, True, id="owner"),
        pytest.param(True, False, "", True, id="owner-with-an-empty-rung"),
        pytest.param(False, True, None, True, id="bound-machine"),
        pytest.param(False, False, "reader", True, id="can-view"),
        pytest.param(False, False, "owner", True, id="full-access"),
        pytest.param(False, False, None, False, id="nobody"),
        pytest.param(False, False, "", False, id="no-grant-reaches-the-caller"),
        pytest.param(False, False, "editor", False, id="a-rung-the-ladder-has-never-heard-of"),
    ],
)
def test_chat_visible_is_owner_or_machine_or_a_rung(
    is_owner: bool, is_bound_machine: bool, shared_role: str | None, visible: bool
) -> None:
    """With the deployment's admin setting off — the shipped default — being an
    org admin changes nothing, so the answer is the three standing cases."""
    for is_org_admin in (False, True):
        assert (
            chat_visible(
                is_owner=is_owner,
                is_bound_machine=is_bound_machine,
                shared_role=shared_role,
                is_org_admin=is_org_admin,
                admin_reads_private=False,
            )
            is visible
        ), is_org_admin


@pytest.mark.parametrize(
    ("is_org_admin", "shared_role", "reason"),
    [
        pytest.param(True, None, "org_admin_reads_private", id="the-admin-the-flag-admits"),
        pytest.param(True, "reader", "shared_reads", id="a-share-outranks-the-flag-as-a-reason"),
        pytest.param(False, None, None, id="a-member-who-is-not-an-admin-is-not-admitted"),
    ],
)
def test_the_admin_branch_is_the_last_reason_and_only_for_an_admin(
    is_org_admin: bool, shared_role: str | None, reason: str | None
) -> None:
    """The one branch a deployment can turn on. It runs LAST, so the reason a
    decision row carries still says what actually opened the chat — a share
    stays "shared_reads" even for an admin the flag would also have admitted."""
    assert (
        chat_read_reason(
            is_owner=False,
            is_bound_machine=False,
            shared_role=shared_role,
            is_org_admin=is_org_admin,
            admin_reads_private=True,
        )
        == reason
    )


def test_the_owner_and_the_machine_outrank_the_flag_as_reasons() -> None:
    """Turning the flag on must not relabel anybody's read: an owner who is also
    an org admin still reads as the owner."""
    assert (
        chat_read_reason(
            is_owner=True,
            is_bound_machine=False,
            shared_role=None,
            is_org_admin=True,
            admin_reads_private=True,
        )
        == "owner_reads"
    )
    assert (
        chat_read_reason(
            is_owner=False,
            is_bound_machine=True,
            shared_role=None,
            is_org_admin=True,
            admin_reads_private=True,
        )
        == "bound_machine_reads"
    )


def test_the_rename_rungs_are_exactly_the_ladders_write_rungs() -> None:
    """``RENAME_ROLES`` is spelled in the authz package rather than imported
    from the Files ladder, and its docstring claims it IS the ladder's write
    row — renaming a node is a write. A rung moved into or out of that row would
    otherwise silently gain or lose the right to retitle every shared chat."""
    assert RENAME_ROLES == frozenset(
        role for role in DEFAULT_LADDER.roles if DEFAULT_LADDER.allows(role, FilesAction.WRITE)
    )
    for role in DEFAULT_LADDER.roles:
        assert role_may_rename(role) is DEFAULT_LADDER.allows(role, FilesAction.WRITE), role
    assert role_may_rename(None) is False
    assert role_may_rename("") is False
    assert role_may_rename("editor") is False


@pytest.mark.parametrize(
    ("owner_user_id", "user_id", "bound_machine_id", "agent_id", "shared_role", "expected"),
    [
        pytest.param(USER, USER, None, None, None, PUBLISHER_ROLE, id="owner-outranks-no-grant"),
        pytest.param(
            USER, USER, None, None, ROLE_READER, PUBLISHER_ROLE, id="owner-outranks-a-weak-grant"
        ),
        pytest.param(
            USER,
            OTHER_USER,
            MACHINE,
            MACHINE,
            ROLE_READER,
            PUBLISHER_ROLE,
            id="the-bound-machine-outranks-its-grant",
        ),
        pytest.param(USER, OTHER_USER, None, None, None, None, id="no-grant-is-no-rung"),
        pytest.param(
            USER, OTHER_USER, None, None, ROLE_READER, ROLE_READER, id="a-reader-grant-reads"
        ),
        pytest.param(
            USER,
            OTHER_USER,
            None,
            None,
            ROLE_COMMENTER,
            ROLE_COMMENTER,
            id="a-commenter-grant-comments",
        ),
        pytest.param(
            USER, OTHER_USER, None, None, ROLE_WRITER, ROLE_WRITER, id="a-writer-grant-writes"
        ),
        pytest.param(
            USER,
            OTHER_USER,
            None,
            None,
            ROLE_OWNER,
            ROLE_OWNER,
            id="the-top-rung-granted-is-still-only-a-grant",
        ),
        pytest.param(
            USER,
            OTHER_USER,
            MACHINE,
            "machine:other",
            ROLE_WRITER,
            ROLE_WRITER,
            id="a-strangers-agent-is-judged-by-its-grant",
        ),
    ],
)
def test_chat_doc_role_table(
    owner_user_id: UUID | None,
    user_id: UUID,
    bound_machine_id: str | None,
    agent_id: str | None,
    shared_role: str | None,
    expected: str | None,
) -> None:
    """A publisher holds the top rung whatever was granted; everybody else
    holds exactly what the node's ACL gave them, and nothing invents a rung out
    of the audience."""
    assert (
        chat_doc_role(
            owner_user_id=owner_user_id,
            user_id=user_id,
            bound_machine_id=bound_machine_id,
            agent_id=agent_id,
            shared_role=shared_role,
        )
        == expected
    )


def test_the_top_rung_on_the_node_is_never_the_documents_publisher() -> None:
    """The pair that keeps a share from becoming a transcript rewrite: the same
    caller who resolves to the top RUNG is still not the PUBLISHER, because
    :func:`chat_writable` does not look at a grant at all."""
    granted = {
        "owner_user_id": USER,
        "user_id": OTHER_USER,
        "bound_machine_id": None,
        "agent_id": None,
    }
    assert chat_doc_role(**granted, shared_role=ROLE_OWNER) == PUBLISHER_ROLE
    assert chat_writable(**granted) is False


def test_an_unknown_rung_is_carried_but_allows_nothing() -> None:
    """Roles are persisted strings and a newer engine may invent rungs this
    build has never heard of. One is reported as it was read — a caller that
    shows it to a person should show the truth — and the ladder allows it
    nothing, so it can never widen what anybody may do."""
    role = chat_doc_role(
        owner_user_id=USER,
        user_id=OTHER_USER,
        bound_machine_id=None,
        agent_id=None,
        shared_role="archivist",
    )
    assert role == "archivist"
    assert DEFAULT_LADDER.allows(role, FilesAction.WRITE) is False
    assert DEFAULT_LADDER.rank(role) == -1
