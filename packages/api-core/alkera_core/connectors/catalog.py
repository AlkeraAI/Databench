"""The driver-free connector descriptor catalog.

The backend and CLI use the same form schemas and builders. The open platform
ships one connector, generic SQL (:data:`GENERIC_SQL`); a distribution adds its
own by registering into :data:`CONNECTORS` during composition. Importing this
module must not load warehouse drivers.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from alkera_core.connectors import generic_sql
from alkera_core.connectors.connection import (
    ConnectionBuild,
    ConnectionBuildResult,
    normalize_connection_build,
)
from alkera_core.connectors.connection_form import ConnectionFormSchema, FormField, ValueSlot
from alkera_core.connectors.primitives import Environment
from alkera_core.connectors.read_only import ReadOnlyEnforcement
from alkera_core.extensions import ExtensionError, ExtensionPoint


@dataclass(frozen=True)
class ConnectorDescriptor:
    """The driver-free surface of one connector plugin."""

    name: str
    """The plugin name — the same key the CLI registry uses (e.g. "snowflake")."""
    title: str
    """Short human label for pickers (e.g. "Snowflake")."""
    form_schema: Callable[[], ConnectionFormSchema]
    """The declarative connection form (auth methods + fields)."""
    build_connection: Callable[[str, str, dict[str, str]], ConnectionBuild]
    """Validate form input into a connection and independently held credentials.
    Raises ``ValueError`` on bad input and never performs I/O."""
    sanitize_shared: Callable[[dict[str, str]], dict[str, str]] | None = None
    """Strip secret material an admin embedded in a NON-secret field's value."""
    distribution_refusal: Callable[[Mapping[str, Any]], str | None] | None = None
    """Why a connection with these built attributes must not be opened by a machine
    other than the one it was typed on (a member's laptop, a cloud box serving many
    orgs), or ``None`` when it may. A connector whose input can name a local file or
    the dialing machine itself registers one; the server refuses such a record at save
    and every machine refuses to materialize one."""
    read_only_enforcement: ReadOnlyEnforcement | None = None
    """What actually keeps this connector from writing, and whether the credential's own
    permissions were checked. ``None`` when the connector has no read-only guarantee of its
    own beyond the agent-side capability gate — an empty badge is the honest answer there,
    and a connector that grows one fills this in rather than the UI inventing a claim."""


#: The connector the open platform ships: any database SQLAlchemy reaches by URL.
GENERIC_SQL = ConnectorDescriptor(
    name="generic_sql",
    title="Generic SQL (SQLAlchemy URL)",
    form_schema=generic_sql.form_schema,
    build_connection=generic_sql.build_connection,
    sanitize_shared=generic_sql.sanitize_shared,
    distribution_refusal=generic_sql.distribution_refusal,
)

#: Every connector the open platform ships, after whatever a distribution registers.
BUILTIN_DESCRIPTORS: tuple[ConnectorDescriptor, ...] = (GENERIC_SQL,)

#: The connectors a distribution adds, in registration order.
CONNECTORS: ExtensionPoint[ConnectorDescriptor] = ExtensionPoint("connectors.descriptors")


def all_descriptors(
    point: ExtensionPoint[ConnectorDescriptor] = CONNECTORS,
) -> tuple[ConnectorDescriptor, ...]:
    """Every connector: those registered on ``point``, then the built-in ones. Freezes
    the point. Two connectors with one name are a wiring error."""
    found = (*point.items(), *BUILTIN_DESCRIPTORS)
    names = [d.name for d in found]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise ExtensionError(f"connectors registered twice on {point.name!r}: {duplicated}")
    return found


def get_descriptor(name: str) -> ConnectorDescriptor:
    """The descriptor for ``name``. Raises ``KeyError`` for a connector that has
    no extracted descriptor (local/file plugins deliberately have none)."""
    for descriptor in all_descriptors():
        if descriptor.name == name:
            return descriptor
    raise KeyError(name)


def team_capable_methods(schema: ConnectionFormSchema) -> list[str]:
    """The auth-method names an admin may preconfigure at team/org level. A
    method-less form (the secret is a shared field) exposes ONE implicit method,
    named the empty string, iff the schema opts in via ``implicit_team_capable``."""
    if not schema.auth_methods:
        return [""] if schema.implicit_team_capable else []
    return [m.name for m in schema.auth_methods if m.team_capable]


def personal_capable_methods(schema: ConnectionFormSchema) -> list[str]:
    """The auth-method names ONE PERSON may configure on a connection of their own.

    Wider than the team set on purpose: a personal connection has no members, so
    a method that only an individual can hold — Google's application-default
    credentials, a local token cache — is not a method that "cannot be
    preconfigured", it is precisely what a personal connection is for. The team
    set stays in, because the org's own browser sign-in is still something the
    owner may pick for themselves. A method-less form always has its implicit
    method here: ``implicit_team_capable`` asks whether an ADMIN may fill a form
    on somebody else's behalf, which is not a question a personal row raises."""
    if not schema.auth_methods:
        return [""]
    return [m.name for m in schema.auth_methods if m.team_capable or m.local_capable]


def local_capable_methods(schema: ConnectionFormSchema) -> list[str]:
    """The auth-method names a member may create ANEW in the local workspace
    form. Excludes methods whose credential belongs to the org (BigQuery's
    OAuth: members never paste a client id/secret — that's team-only)."""
    return [m.name for m in schema.auth_methods if m.local_capable]


def local_form(schema: ConnectionFormSchema) -> ConnectionFormSchema:
    """The LOCAL workspace view of a form — only the ``local_capable`` auth
    methods. A method-less form is unchanged. When filtering leaves no method
    (every method is team-only), the form still renders its shared fields so a
    detect-then-add / ADC-only connector isn't hidden."""
    if not schema.auth_methods:
        return schema
    kept = [m for m in schema.auth_methods if m.local_capable]
    if len(kept) == len(schema.auth_methods):
        return schema
    return schema.model_copy(update={"auth_methods": kept})


def form_fields(schema: ConnectionFormSchema, auth_method: str) -> list[FormField]:
    """Every input the form renders for one auth method — the method's own
    fields followed by the shared ones. A method-less form (the implicit
    method, named the empty string) renders the shared fields alone."""
    method = next((m for m in schema.auth_methods if m.name == auth_method), None)
    return [*(method.fields if method is not None else []), *schema.shared_fields]


def is_oauth_method(schema: ConnectionFormSchema, auth_method: str) -> bool:
    """Whether this auth method completes through a browser sign-in."""
    method = next((m for m in schema.auth_methods if m.name == auth_method), None)
    return method is not None and method.oauth is not None


# ---------------------------------------------------------------------------
# The team-preconfiguration surface
# ---------------------------------------------------------------------------

#: The deployment tier, asked as an ordinary form field so it carries the same
#: distribute-or-not switch as every other value. Nothing derives it: a tier read
#: off a hostname is a guess about somebody else's warehouse, and every tool that
#: gets this right (dbt Cloud, GitHub Actions, AWS) takes it as a declaration.
ENVIRONMENT_FIELD = FormField(
    name="environment",
    label="Deployment tier",
    type="select",
    required=True,
    enum_values=[str(e) for e in Environment],
    enum_labels={
        "prod": "Production",
        "staging": "Staging",
        "dev": "Development",
        "local": "Local",
    },
    # Its own heading, so the optional run visibly ends before it. A required
    # field reading as one more optional default is a false boundary.
    group="Deployment",
    # No description: the label is the whole question, and the form explains the
    # prefill switch once, above.
    help="",
)


def team_form_schema(schema: ConnectionFormSchema) -> ConnectionFormSchema:
    """The TEAM view of a form: the connector's own fields plus the deployment
    tier. The local workspace form never shows it — a member adding their own
    connection is not distributing anything."""
    return schema.model_copy(update={"trailing_fields": [ENVIRONMENT_FIELD]})


def team_form_fields(schema: ConnectionFormSchema, auth_method: str) -> list[FormField]:
    """Every input the TEAM form collects for one auth method, in render order."""
    return [*form_fields(schema, auth_method), ENVIRONMENT_FIELD]


def ask_groups(specs: Sequence[FormField]) -> list[list[str]]:
    """The completeness rule over ``specs``, compiled to a structural shape:
    each group lists the input names of which AT LEAST ONE must be answered.

    A plain required field is a singleton group; a required field with declared
    alternatives leads a group containing them. Clients never re-derive this —
    they receive the compiled groups and check them against values they hold,
    so only this module ever reads ``required`` together with ``satisfied_by``."""
    return [[f.name, *f.satisfied_by] for f in specs if f.required]


def missing_required(specs: Sequence[FormField], fields: dict[str, str]) -> list[FormField]:
    """The required inputs among ``specs`` whose ask group ``fields`` leaves
    entirely blank.

    Presence is checked HERE rather than inside each connector so every surface
    refuses the same way and says the same thing. Callers choose the specs:
    ``form_fields`` for a workspace connection, ``team_form_fields`` where the
    deployment tier is part of the answer."""

    def answered(name: str) -> bool:
        return bool((fields.get(name) or "").strip())

    by_name = {f.name: f for f in specs}
    return [
        by_name[group[0]]
        for group in ask_groups(specs)
        if not any(answered(name) for name in group)
    ]


def required_error(missing: Sequence[FormField]) -> str:
    """One sentence naming what is still blank, by the label the user read."""
    names = ", ".join(f.label for f in missing)
    return f"{names} is required" if len(missing) == 1 else f"{names} are required"


def overlong_values(specs: Sequence[FormField], fields: dict[str, str]) -> list[FormField]:
    """The inputs among ``specs`` whose value exceeds their declared bound.
    Enforced through the same compile as presence so a bound declared once on
    the field holds on every surface that accepts a value for it."""
    return [
        f for f in specs if f.max_length and len((fields.get(f.name) or "").strip()) > f.max_length
    ]


def length_error(overlong: Sequence[FormField]) -> str:
    """One sentence naming what is too long, by label, with the bound."""
    return ". ".join(f"{f.label} must be {f.max_length} characters or fewer" for f in overlong)


def values_doc(
    specs: Sequence[FormField],
    *,
    values: dict[str, str],
    member_names: frozenset[str] | set[str],
    deferred: frozenset[str] | set[str] = frozenset(),
) -> list[ValueSlot]:
    """The values document for one connection: every input of ``specs`` as a
    slot in form order, carrying its owner and its answer.

    ``values`` never lands on a secret slot — a secret's answer travels as
    ``deferred`` (stored encrypted, on disk, or behind a sign-in). Values whose
    name no spec declares trail the document labeled by their own name, so a
    row written by a newer catalog still shows everything it distributes."""
    known = {f.name for f in specs}
    doc = [
        ValueSlot(
            name=f.name,
            label=f.label,
            value="" if f.secret else (values.get(f.name) or ""),
            owner="member" if f.name in member_names else "admin",
            deferred=f.name in deferred,
            secret=f.secret,
            type=f.type,
            required=f.required,
            default=f.default,
            enum_values=list(f.enum_values),
            enum_labels=dict(f.enum_labels),
            help=f.help,
            placeholder=f.placeholder,
            group=f.group,
            max_length=f.max_length,
        )
        for f in specs
    ]
    doc += [
        ValueSlot(name=name, label=name, value=str(value))
        for name, value in values.items()
        if name not in known
    ]
    return doc


def doc_shared_values(doc: Sequence[ValueSlot]) -> dict[str, str]:
    """Flatten a values document to the stored ``shared_values`` column: the
    admin-owned, non-secret answers by input name."""
    return {s.name: s.value for s in doc if s.owner == "admin" and not s.secret}


def doc_member_fields(doc: Sequence[ValueSlot]) -> list[str]:
    """Flatten a values document to the stored ``member_fields`` column: the
    input names each member answers themselves, in form order."""
    return [s.name for s in doc if s.owner == "member"]


def build_result(
    descriptor: ConnectorDescriptor,
    handle: str,
    auth_method: str,
    fields: dict[str, str],
) -> ConnectionBuildResult:
    """Build the canonical connection result, including independent named secrets."""
    result = normalize_connection_build(descriptor.build_connection(handle, auth_method, fields))
    tier = (fields.get(ENVIRONMENT_FIELD.name) or "").strip()
    if not tier:
        return result
    try:
        environment = Environment(tier)
    except ValueError:
        raise ValueError(f"unknown deployment tier {tier!r}") from None
    return ConnectionBuildResult(
        connection=result.connection.model_copy(update={"environment": environment}),
        credential=result.credential,
        named_credentials=result.named_credentials,
    )


def built_attributes(
    descriptor: ConnectorDescriptor,
    handle: str,
    auth_method: str,
    fields: dict[str, str],
) -> dict[str, str]:
    """The dialable attributes of a stored row, built from the values it
    distributes. A row holds the admin's raw form input, not a built
    connection, so a server that needs coordinates (a probe, an OAuth relay)
    turns the row into them exactly as a member's machine does.

    Raises ``ValueError`` on invalid input, like :func:`build_result`."""
    result = build_result(descriptor, handle, auth_method, fields)
    return {str(k): str(v) for k, v in result.connection.attributes.items()}


def sanitized_shared_values(
    descriptor: ConnectorDescriptor, fields: dict[str, str]
) -> dict[str, str]:
    """``fields`` with any secret the admin embedded in a non-secret value
    removed, ready to be stored and handed to every member."""
    if descriptor.sanitize_shared is None:
        return dict(fields)
    return descriptor.sanitize_shared(fields)


def distribution_refusal(
    descriptor: ConnectorDescriptor, attributes: Mapping[str, Any]
) -> str | None:
    """Why a built connection must not be distributed to other machines, or ``None``
    (:attr:`ConnectorDescriptor.distribution_refusal`)."""
    if descriptor.distribution_refusal is None:
        return None
    return descriptor.distribution_refusal(attributes)


def credential_role_for(spec: FormField) -> str:
    """Return the field's named role or the implicit primary role."""
    return spec.credential_role or "primary"


def credential_custody(
    descriptor: ConnectorDescriptor,
    auth_method: str,
    shared_field_names: Sequence[str],
    credential_roles: Collection[str],
) -> dict[str, str]:
    """Map credential roles to their admin or member owner.

    Browser sign-in stays with the member. Otherwise, the admin owns an implicit
    role or one with a shared source. The member owns a role whose sources are
    all held back.
    """
    shared = set(shared_field_names)
    specs = form_fields(descriptor.form_schema(), auth_method)
    secret_roles = {credential_role_for(spec) for spec in specs if spec.secret}
    sources: dict[str, set[str]] = {}
    for spec in specs:
        role = credential_role_for(spec)
        carried_by_a_nonsecret_field = (
            bool(spec.credential_role) and role not in secret_roles and auth_method != "none"
        )
        if spec.secret or carried_by_a_nonsecret_field:
            sources.setdefault(role, set()).add(spec.name)
    roles = set(credential_roles) | set(sources)
    custody: dict[str, str] = {}
    for role in roles:
        role_sources = sources.get(role, set())
        owned_by_admin = not role_sources or not role_sources.isdisjoint(shared)
        custody[role] = "admin" if owned_by_admin else "member"
    return custody


def admin_answered_secret_fields(
    specs: Sequence[FormField],
    stored_roles: Collection[str],
    custody: Mapping[str, str],
) -> set[str]:
    """Return admin-owned secret fields backed by a stored credential role."""
    stored = set(stored_roles)
    answered: set[str] = set()
    for spec in specs:
        role = credential_role_for(spec)
        if spec.secret and role in stored and custody.get(role) == "admin":
            answered.add(spec.name)
    return answered


def auth_mode_for(
    schema: ConnectionFormSchema, auth_method: str, custody: Mapping[str, str]
) -> str:
    """Return who holds a credential from its catalog-owned custody map."""
    if is_oauth_method(schema, auth_method):
        return "per_user"
    return "shared" if not custody or "admin" in custody.values() else "per_user"


def is_auto_add_eligible(
    schema: ConnectionFormSchema, auth_method: str, member_fields: Sequence[str]
) -> bool:
    """Whether this connection may be flagged auto-add. Derived: a connection
    materializes without the member touching it only when the admin distributed
    every value and no browser sign-in stands in the way. A method's
    ``auto_add_override`` force-overrides the derivation."""
    method = next((m for m in schema.auth_methods if m.name == auth_method), None)
    if method is not None and method.auto_add_override is not None:
        return method.auto_add_override
    return not member_fields and not is_oauth_method(schema, auth_method)


__all__ = [
    "BUILTIN_DESCRIPTORS",
    "CONNECTORS",
    "ENVIRONMENT_FIELD",
    "GENERIC_SQL",
    "ConnectorDescriptor",
    "admin_answered_secret_fields",
    "all_descriptors",
    "ask_groups",
    "auth_mode_for",
    "build_result",
    "credential_custody",
    "credential_role_for",
    "distribution_refusal",
    "doc_member_fields",
    "doc_shared_values",
    "form_fields",
    "get_descriptor",
    "is_auto_add_eligible",
    "is_oauth_method",
    "length_error",
    "local_capable_methods",
    "local_form",
    "missing_required",
    "overlong_values",
    "personal_capable_methods",
    "required_error",
    "sanitized_shared_values",
    "team_capable_methods",
    "team_form_fields",
    "team_form_schema",
    "values_doc",
]
