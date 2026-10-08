"""Closed vocabularies of the authorization model.

Every enum here is a ``StrEnum`` so it serializes to its lowercase value, compares
against a string literal, and can back a VARCHAR column later without a native
Postgres enum. The values are wire and storage contracts (decision rows, role
assignments, audit documents): renaming one is a schema change, not a refactor.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class PrincipalKind(StrEnum):
    """What sort of thing is acting."""

    USER = "user"
    AGENT = "agent"
    SERVICE = "service"
    PAT = "pat"
    # A chat box speaking on its own machine credential: no user behind it,
    # no roles, and a reach of exactly the resources it holds.
    MACHINE = "machine"


class CredentialKind(StrEnum):
    """How a principal proved (or, for an agent, merely asserted) its identity."""

    JWT = "jwt"
    CI_TOKEN = "ci_token"  # noqa: S105 -- names a credential kind, not a credential
    PROXY_TOKEN = "proxy_token"  # noqa: S105 -- names a credential kind, not a credential
    PAT = "pat"
    AGENT_HEADER = "agent_header"
    # A platform machine credential: a chat box speaking as itself.
    MACHINE = "machine"
    # A short-lived credential the box's machine credential mints for ONE org:
    # the process that serves that org's chats speaks on it, and it reaches
    # nothing outside that org.
    MACHINE_WORKER = "machine_worker"


class Role(StrEnum):
    """A role a subject holds at some scope.

    ``VIEWER < MEMBER < ADMIN < OWNER`` is a ladder: a higher rung implies every
    rung below it. ``AGENT`` and ``SERVICE`` are kind markers rather than rungs —
    they say *what* the subject is, imply only themselves, and never grant a
    human role by implication.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"
    AGENT = "agent"
    SERVICE = "service"

    def implies(self, other: Role) -> bool:
        """``True`` when holding ``self`` also means holding ``other``."""
        if self is other:
            return True
        if self in _LADDER and other in _LADDER:
            return _LADDER.index(self) > _LADDER.index(other)
        return False


#: The human-role ladder, lowest rung first.
_LADDER: tuple[Role, ...] = (Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER)


def expand_roles(roles: Iterable[Role]) -> frozenset[Role]:
    """Close a set of roles under :meth:`Role.implies`.

    ``{OWNER}`` expands to ``{OWNER, ADMIN, MEMBER, VIEWER}``; ``{AGENT}`` stays
    ``{AGENT}``. Policies test membership against the expansion so they never
    have to spell the ladder themselves.
    """
    held = tuple(roles)
    return frozenset(implied for role in held for implied in Role if role.implies(implied))


class ScopeKind(StrEnum):
    """Where a role assignment applies."""

    ORG = "org"
    TEAM = "team"


class ResourceType(StrEnum):
    """The kinds of thing a policy can decide about."""

    CONNECTOR = "connector"
    BILLING_POOL = "billing_pool"
    KB_ITEM = "kb_item"
    GATE_RUN = "gate_run"
    # A GitHub App installation an org has claimed for the gate.
    GITHUB_INSTALLATION = "github_installation"
    ORG = "org"
    # An org's audit trail, as a client writes into it: a member's daemon
    # reporting what its agent did. Its own type because the decision is
    # about the trail (membership and the plan it belongs to), not about the
    # org-wide integrations ORG answers for.
    ORG_AUDIT = "org_audit"
    TEAM = "team"
    # One person's standing on one team: the row written there and the role
    # that reaches it from a team above.
    TEAM_MEMBERSHIP = "team_membership"
    # A team's own allowance of budget or storage, set from above the team.
    TEAM_ALLOCATION = "team_allocation"
    # One member's storage cap inside one team's folder, set by that team's
    # admins within what the team was allocated.
    TEAM_STORAGE_CAP = "team_storage_cap"
    CHAT = "chat"
    # A saved chat: the brief and the starting files a new chat is cut from.
    # Its own type because it is shared, edited and started from on terms a
    # chat's policy does not answer — a reader of a template starts chats from
    # it without ever reading the conversation it was distilled from.
    CHAT_TEMPLATE = "chat_template"
    # Chats that share one file tree: read through the rung on its folder,
    # which every chat in it inherits, and owned by the person whose
    # connections its chats use.
    WORKSPACE = "workspace"
    ARTIFACT = "artifact"
    WORKSPACE_OBJECT = "workspace_object"
    # The compute surface: the catalog, a session allocation, the org's workspace machine.
    COMPUTE_MACHINE = "compute_machine"
    # An org's standing permission to run machines — a platform-staff object,
    # decided about the platform rather than about the tenant it names.
    COMPUTE_GRANT = "compute_grant"
    # A machine an org holds: read by its managers and its audience, managed by
    # org admins and admins of the team holding it or any team above.
    ORG_MACHINE = "org_machine"
    # What Alkera sells: written by platform admins, decided about the platform.
    COMPUTE_OFFERING = "compute_offering"
    # Where a workspace runs: moving it needs full access on the workspace and
    # use of the machine it goes to.
    WORKSPACE_MACHINE = "workspace_machine"
    # One node of the Files tree: a file, a folder, a symlink, an object.
    FILE_NODE = "file_node"
    # The kind of lease taken on, or forced off, one folder: a workspace's
    # lease is its bound box's alone, whatever rung admits the caller.
    FILE_LEASE = "file_lease"
    # A notebook file, as something that runs: its kernel executes the code
    # in it, which is decided apart from reading or editing the file.
    NOTEBOOK = "notebook"
    # A platform ban on a user or an email domain — decided about the platform,
    # like a compute grant, never about the tenant the banned person belongs to.
    PLATFORM_BAN = "platform_ban"
    # An org's storage ceiling set by hand — the platform's decision about a
    # tenant, decided about the platform like a ban is.
    PLATFORM_ORG_STORAGE = "platform_org_storage"
    # Whether an org's files open live (co-edited) set by hand — the platform's
    # operational decision about a tenant, decided about the platform.
    PLATFORM_ORG_LIVE_EDITING = "platform_org_live_editing"
    # Which email domains an org's SSO speaks for — a namespace every org
    # shares, so the platform assigns it, never the tenant.
    PLATFORM_ORG_SSO_DOMAINS = "platform_org_sso_domains"
    # Which org the deployment's own Slack workspace belongs to — the operator's
    # credential, so the decision is the platform's, not the tenant's.
    PLATFORM_ORG_SLACK = "platform_org_slack"
    # A platform-owned chat box: its credential, its registration, and which
    # org (if any) it is dedicated to — decided about the platform.
    PLATFORM_MACHINE = "platform_machine"
    # A box acting on its own machine credential: claiming and heartbeating
    # the one machine the credential names, and nothing else.
    MACHINE_CREDENTIAL = "machine_credential"
    # Alkera's own money moved by hand: credit granted or scheduled, a spend cap
    # on a pool member, the prices a model sells at and costs. Decided about the
    # platform — it is Alkera's provider keys being spent, not the tenant's.
    PLATFORM_BILLING = "platform_billing"
    # One person's account as a whole: exporting everything about it and
    # deleting it. Decided about the identity, never about an org it is in.
    ACCOUNT = "account"
    # A box on a person's own hardware, as its person sees it: listing their
    # boxes and taking one's standing away. Decided about the identity.
    PERSONAL_BOX = "personal_box"


class Action(StrEnum):
    """What the caller is trying to do to a resource."""

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"
    CREATE = "create"
    SEND = "send"
    EXPORT = "export"
    RERUN = "rerun"
    UPLOAD_PAYLOAD = "upload_payload"
    FETCH_CREDENTIAL = "fetch_credential"
    SET_BUDGET = "set_budget"
    PROMOTE = "promote"
    DELETE = "delete"
    # Taking the chat warmed ahead for oneself: neither a read (nobody but the
    # owner may so much as learn a spare exists) nor a create (the row is
    # already there), so it is decided and recorded under its own name.
    CLAIM = "claim"
    # Re-addressing a resource to a different owner, which is neither a write to
    # its contents nor a delete: a decision row says so rather than reading as an
    # ordinary edit that happened to change one column.
    MOVE = "move"
    # Changing what a resource is CALLED. Its own verb because a chat's name is
    # the one part of it a collaborator changes without touching the transcript:
    # WRITE on a chat is the publishing machine's, so renaming had nowhere to be
    # decided and ended up being decided somewhere else, by a different rule.
    RENAME = "rename"
    # Reading the connections a chat's agent may use: the set its OWNER is
    # entitled to, answered to the machine that holds the chat. Its own verb
    # because it is neither a read of the transcript nor a person's door — a
    # decision row that said READ would hide which of the two the box opened.
    LIST_CONNECTIONS = "list_connections"
    # Leasing, for the machine that holds a chat, the credential of one
    # connection the chat's OWNER may use. Its own verb because the list above
    # hands out names and this hands out a secret: an auditor reading a
    # decision row must be able to tell the two apart without the route.
    LEASE_CONNECTION_CREDENTIAL = "lease_connection_credential"
    # Answering an ask with a STANDING option ("Always allow", "Always
    # reject"). Its own verb because the answer outlives the ask: it becomes a
    # rule the agent applies in its owner's other chats too, so it is a
    # decision for the person whose rule it becomes, not for whoever may speak.
    ANSWER_STANDING = "answer_standing"
    # The Files verbs. Each names a distinct thing a caller does to a node, so
    # a decision row says which one was refused rather than lumping them into
    # WRITE; the values match ``FilesAction`` so the ladder's allowed-action
    # names and the platform action share one vocabulary.
    SHARE = "share"
    RESTORE = "restore"
    LEASE = "lease"
    LEASE_REQUEST = "lease_request"
    LEASE_FORCE = "lease_force"
    SNAPSHOT = "snapshot"
    LOCK = "lock"
    HOLD = "hold"
    COMMENT = "comment"
    COPY = "copy"
    # Executing code: a notebook's run, and the kernel and widget requests
    # that act through its kernel. Its own verb because running is not
    # editing: a decision row must say code was run, not that a file changed.
    RUN = "run"


class Effect(StrEnum):
    """The outcome of a decision."""

    ALLOW = "allow"
    DENY = "deny"


__all__ = [
    "Action",
    "CredentialKind",
    "Effect",
    "PrincipalKind",
    "ResourceType",
    "Role",
    "ScopeKind",
    "expand_roles",
]
