"""The typed ``Tool`` abstraction + the ``ToolRegistry``.

The arg contract is a Pydantic model, so the LLM-facing JSON-Schema is
*derived* (one source of truth, never drifts), the handler receives a
*validated typed object* (not a ``dict``), and variant call signatures are
a discriminated union the type-checker enforces exhaustively.

Beside ``Tool`` / ``ToolSpec`` / ``ToolContext`` / ``ToolError`` and the
registry *storage* (register / connection_for / specs), the registry owns
the daemon-side selection — there is NO provider-native Tool Search and NO
enable/defer path:

- ``hot_prefix()`` — the FIXED hot set (the few first-class tools + the two
  meta-tools ``search_tools`` + ``call_tool``), composed ONCE per session and
  never mutated, so the prompt cache is fully preserved.
- ``search(query, app?, k)`` — an in-process BM25 index over the active
  catalog (tool name + description + param names + app namespace, with a
  structural app pre-filter). Backs the ``search_tools`` meta-tool; its result
  (top-k tool schemas) lands in the messages region.
- ``dispatch(name, args)`` — the ``call_tool`` core: look up → validate args
  against the tool's ``Input`` → broker-gate write effects → run → serialize.
  The ONLY way any non-hot tool is invoked.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final, Generic, TypeVar

from alkera_core.extensions import ExtensionPoint
from pydantic import BaseModel, ConfigDict, ValidationError

from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base._bm25 import Bm25Index, tokenize
from alkera_cli.plugins.plugin_base.delivery import deliver_generic, with_conflict_notices
from alkera_cli.plugins.plugin_base.wire import (
    serialize_tool_result,
    tool_error_result,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from alkera_core.project.chats.blobs import BlobStore

    from alkera_cli.environment import ChatRunnerFactory, EnvironmentService
    from alkera_cli.plugins.plugin_base.agent_result import SubagentRunResult
    from alkera_cli.plugins.plugin_base.capabilities import CapabilitySet
    from alkera_cli.plugins.plugin_base.connection import Connection
    from alkera_cli.plugins.plugin_base.surfaces import AgentDefinition

TIn = TypeVar("TIn", bound=BaseModel)
TOut = TypeVar("TOut", bound=BaseModel)


#: The tools that run arbitrary code in a child process with no path fence
#: around it (``call_graph_python``, ``call_integration_sdk``). A bounded (cloud)
#: session withholds them — from the advertised list and at dispatch — until
#: the child runs under the chat's own identity; the fenced shell is the one
#: way to run code there.
FENCED_CODE_TOOLS: frozenset[str] = frozenset({"call_graph_python", "call_integration_sdk"})
FENCED_CODE_TOOL_REASON = (
    "not available in a cloud chat: it runs code outside the chat's workspace "
    "fence; use the bash tool inside this chat's folder instead"
)

#: ``ToolSpec.app`` of every agent-spawning tool. Membership is what the
#: subagents switch keys off, so a tool added to that family is withheld by
#: declaring the app, not by editing a list.
SUBAGENT_TOOL_APP = "agent"
#: The agent-spawning tools by name, for the dispatch refusal — a call can
#: arrive for a tool the registry never registered (a resumed session, a
#: replayed transcript), and "unknown tool" would read as a typo rather than a
#: deliberate deployment choice. Kept in step with the generated tool manifest
#: by ``test_subagents_disabled.py``.
SUBAGENT_TOOL_NAMES: frozenset[str] = frozenset({"spawn_agent", "list_agent_types"})
SUBAGENT_DISABLED_REASON = (
    "Subagents are disabled on this deployment: delegation is turned off here, "
    "so do the work in this chat yourself."
)


#: What a wrong-typed value was expected to be, by the pydantic error type's prefix.
_EXPECTED_BY_TYPE: tuple[tuple[str, str], ...] = (
    ("int_", "an integer"),
    ("float_", "a number"),
    ("decimal_", "a number"),
    ("bool_", "true or false"),
    ("string_", "a string"),
    ("list_", "a list"),
    ("tuple_", "a list"),
    ("set_", "a list"),
    ("dict_", "an object"),
    ("model_", "an object"),
    ("date_", "a date"),
    ("datetime_", "a date and time"),
    ("uuid_", "a UUID"),
)
#: How many problems one message names before it says how many more there are.
_MAX_VALIDATION_PROBLEMS = 8
#: How much of a scalar the message quotes back.
_MAX_QUOTED_VALUE = 40


def _quoted_value(value: Any) -> str:
    """A short spelling of the value the caller sent: a scalar quoted and bounded,
    anything larger named by its kind, so the message never echoes an input."""
    if isinstance(value, bool) or value is None:
        return "true" if value is True else "false" if value is False else "null"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        text = value if len(value) <= _MAX_QUOTED_VALUE else value[: _MAX_QUOTED_VALUE - 1] + "…"
        return repr(text)
    if isinstance(value, list | tuple):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


#: The ``loc`` elements pydantic adds for one member of a plain union (``str``,
#: ``int``, ``list[str]``) rather than for a field the caller named.
_UNION_MEMBER_NAMES = frozenset(
    {"str", "int", "float", "bool", "bytes", "date", "datetime", "time", "decimal", "none"}
)


#: pydantic's constraint messages ("input should be greater than 0", "string
#: should have at most 400 characters"), reworded as what the field must be.
_SHOULD = re.compile(r"^(?:input|string|list|value|dictionary|tuple|set) should (.+)$")


def _is_union_member(part: Any) -> bool:
    return isinstance(part, str) and (part in _UNION_MEMBER_NAMES or "[" in part)


def _expected(kind: str) -> str | None:
    """What a wrong-typed value should have been, or ``None`` for another kind."""
    if not (kind.endswith("_type") or kind.endswith("_parsing")):
        return None
    for prefix, expected in _EXPECTED_BY_TYPE:
        if kind.startswith(prefix):
            return expected
    return None


def _either(options: list[str]) -> str:
    return options[0] if len(options) == 1 else ", ".join(options[:-1]) + " or " + options[-1]


@dataclass(slots=True)
class _Problem:
    loc: tuple[Any, ...]
    kind: str
    expected: list[str]
    value: Any
    message: str
    context: dict[str, Any]


def _root_union_tags(model: type[BaseModel]) -> frozenset[str]:
    """The tags of the discriminated union an Input is rooted on, or none.

    pydantic opens the path of every error inside such a union with the tag of
    the variant it tried (``("describe", "table")``). That tag is not a field
    the caller sends, and spelled into the message as ``describe.table`` it
    reads as a nested object the model then tries to build."""
    discriminator = model.model_json_schema().get("discriminator")
    if not isinstance(discriminator, dict):
        return frozenset()
    mapping = discriminator.get("mapping")
    return frozenset(mapping) if isinstance(mapping, dict) else frozenset()


def _problems(errors: list[Any], union_tags: frozenset[str] = frozenset()) -> list[_Problem]:
    """One problem per error, with the root union's tag dropped from each path
    and the members of one plain union folded into a single problem at the
    union's own path."""
    folded: list[_Problem] = []
    for error in errors:
        loc = tuple(error.get("loc", ()))
        if loc and loc[0] in union_tags:
            loc = loc[1:]
        kind = str(error.get("type", ""))
        expected = _expected(kind)
        member = bool(loc) and _is_union_member(loc[-1])
        if expected is not None and member:
            loc = loc[:-1]
            if folded and folded[-1].loc == loc and folded[-1].expected:
                if expected not in folded[-1].expected:
                    folded[-1].expected.append(expected)
                continue
        folded.append(
            _Problem(
                loc=loc,
                kind=kind,
                expected=[expected] if expected is not None else [],
                value=error.get("input"),
                message=str(error.get("msg", "is invalid")).rstrip("."),
                context=dict(error.get("ctx") or {}),
            )
        )
    return folded


def _problem_sentence(tool: str, problem: _Problem) -> str:
    path = ".".join(str(part) for part in problem.loc)
    if problem.kind == "missing":
        return f"{tool} needs `{path}`." if path else f"{tool} needs its arguments."
    if problem.kind == "extra_forbidden":
        return f"{tool} does not take `{path}`."
    if problem.kind == "union_tag_invalid":
        field = str(problem.context.get("discriminator", "")).strip("'")
        tags = str(problem.context.get("expected_tags", "")).replace(", ", " or ")
        tag = _quoted_value(problem.context.get("tag"))
        return f"`{field}` must be {tags}, not {tag}."
    if problem.expected and path:
        return f"`{path}` must be {_either(problem.expected)}, not {_quoted_value(problem.value)}."
    message = problem.message.removeprefix("Value error, ")
    message = f"{message[:1].lower()}{message[1:]}"
    should = _SHOULD.match(message)
    if should is not None and path:
        return f"`{path}` must {should.group(1)}."
    if not path:
        return f"{tool} arguments are invalid: {message}."
    return f"`{path}` is invalid: {message}."


def describe_validation_error(
    tool: str, exc: ValidationError, input_model: type[BaseModel] | None = None
) -> str:
    """A tool's argument validation failure as one plain sentence per problem —
    the field path and what is wrong with it, with no input echoed back and no
    documentation links, so the model can fix the call from the text alone.
    ``input_model`` is the tool's Input, whose root union tags are not part of
    any path the caller can send."""
    tags = _root_union_tags(input_model) if input_model is not None else frozenset()
    errors = _problems(exc.errors(include_url=False), tags)
    sentences = [_problem_sentence(tool, problem) for problem in errors[:_MAX_VALIDATION_PROBLEMS]]
    extra = len(errors) - _MAX_VALIDATION_PROBLEMS
    if extra > 0:
        sentences.append(f"{extra} more problem{'s' if extra != 1 else ''} not shown.")
    return " ".join(dict.fromkeys(sentences)) or f"{tool} got invalid arguments."


class ToolError(Exception):
    """A recoverable tool failure; the dispatcher returns it to the model as a tool-error result."""

    def __init__(self, message: str, classification: str | None = None) -> None:
        super().__init__(message)
        self.classification: str | None = classification


#: Name the kind of failure an exception is (``"timeout"``, ``"permission"``, ...)
#: or answer ``None`` to leave it to the next one. Registered by the distribution
#: that knows the exceptions its tools raise; a failure nobody names is an
#: ``"error"``.
TOOL_FAILURE_CLASSIFIERS: ExtensionPoint[Callable[[BaseException], str | None]] = ExtensionPoint(
    "tool_failure_classifiers"
)


def classify_tool_failure(exc: BaseException) -> str:
    """A ToolError classifies by its explicit field, else its cause, never its
    own sentence; any other exception by the first registered classifier that
    names it."""
    if isinstance(exc, ToolError):
        if exc.classification is not None:
            return exc.classification
        return classify_tool_failure(exc.__cause__) if exc.__cause__ else "error"
    for classify in TOOL_FAILURE_CLASSIFIERS.items():
        found = classify(exc)
        if found is not None:
            return found
    return "error"


class ToolSpec(BaseModel):
    """Runtime metadata for a tool. Serialized to the wire as the tool's
    advertised shape; NOT persisted to the chat log (the log stores tool
    *data* — name+input+result — never the spec), so a plain BaseModel."""

    model_config = ConfigDict(frozen=True)

    name: str
    """Namespaced — e.g. "sql.query", "snowflake.unload_to_stage"."""
    title: str = ""
    description: str = ""
    hot: bool = False
    """In the ≤~20 always-on prefix vs the deferred long tail."""
    effect_hint: Effect = Effect.READ
    """Advisory; the real gate is the connector + broker at run time."""
    app: str | None = None
    """Owning app namespace, for structural search ranking."""
    deliver_whole: bool = False
    """Exempt this tool's result from the inline size cap: it reaches the model
    whole, never replaced by a preview plus a handle to fetch. For the result
    that IS the compression — a subagent's report stands for an entire child
    session the caller never sees — where spilling turns a finished answer back
    into an errand. Every other tool goes through the door."""


#: A session's tool restriction (subagent ``tool_scope``): ``None``/``[]`` = the
#: full set; ``"read_only"`` = only ``effect_hint == READ`` tools; a list of names
#: = exactly those tools. Lets an ``explore`` subagent see only read tools while a
#: ``worker`` keeps the full set (the per-subagent-type permission control).
ToolScope = list[str] | str | None


def tool_in_scope(spec: ToolSpec, tool_scope: ToolScope) -> bool:
    """Whether ``spec`` is exposed to / runnable by a session with ``tool_scope``."""
    if not tool_scope:  # None or empty list → unrestricted
        return True
    if tool_scope == "read_only":
        return spec.effect_hint == Effect.READ
    if isinstance(tool_scope, (list, tuple, set)):
        return spec.name in tool_scope
    return True


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What every tool handler receives. A runtime bag of LIVE, non-
    serializable handles (broker, registry, blobs) — hence a dataclass, not
    a persisted model. Built per tool call by ``ToolRegistry.build_context``.
    """

    registry: ToolRegistry
    blobs: BlobStore
    connection: Connection | None = None
    broker: Any = None
    """The session ``PermissionBroker`` — write tools gate HERE (MCP tools
    auto-allow, so the gate lives in the tool body, not ``_can_use_tool``).
    Typed loosely to keep ``plugin_base`` decoupled from the harness layer."""
    cap_token_mint: Callable[..., Awaitable[Any]] | None = None
    """Mints a ``CapToken`` after broker approval."""
    gateway: Any = None
    """Metered LLM/tool execution client. ``None`` until a tool needs it."""
    permission_mode: str = "default"
    """The session's active mode (read_only/default/auto/plan/bypass) — the SQL
    gate's policy reasons over it."""
    permissions: Any = None
    """The loaded ``.alkera/permissions.yml`` ``PermissionsConfig`` (rules +
    per-connection environment), or ``None``. Typed loosely to keep this module
    decoupled from the permissions package."""
    session_id: str = ""
    """The chat session id — stamped onto a ``PermissionRequest`` the gate emits
    and used as the per-chat cost-ledger key."""
    owner_session_id: str = ""
    """The session whose sandbox identity owns the working tree this session
    runs in: the session itself, or for a subagent child its parent, whose root
    and uid the child shares. Every file a tool makes in the root is handed to
    this session's identity, never the child's own. Empty means the session."""
    spawn: Callable[..., Awaitable[SubagentRunResult]] | None = None
    """Spawn a subagent: ``await ctx.spawn(prompt, agent=..., description=...)``
    → a ``SubagentRunResult`` (summary + usage stats, or ``error``). ``None`` for a
    subagent session (a subagent cannot spawn) or when no session is bound."""
    cost_ledger: Any = None
    """The project ``CostLedger`` — the resolver chokepoint meters warehouse-query
    cost through it (estimate → cap check → settle). ``None`` disables metering
    (no ledger bound). Typed loosely to keep ``plugin_base`` decoupled."""
    decision_sink: Any = None
    """The project ``DecisionSink`` — the SQL gate appends its decision here so the
    ``.alkera/decisions.jsonl`` audit captures data-tool approvals too. ``None``
    disables auditing. Typed loosely to keep ``plugin_base`` decoupled."""
    judge: Any = None
    """The auto-mode ``SafetyJudge`` — the in-tool gate grounds its recoverable-write
    middle through it, exactly like the harness loop, so auto mode is judged here
    too. ``None`` → the deterministic policy stands. Typed loosely (no harness dep)."""
    task_goal: str = ""
    """The current turn's user message — the one-line context the judge reasons over."""
    alkera_dir: Any = None
    """The ``.alkera/`` dir (a ``Path``) — enables always-allow/reject persistence
    from an in-tool prompt, same as the harness loop."""
    task_store: Any = None
    """The session's per-chat ``TaskStore`` (the unified TODO system) — the
    ``manage_tasks`` tool reads/mutates it. ``None`` for a subagent (the tool is
    gated out) or when no chat is bound. Typed loosely to keep ``plugin_base``
    decoupled from ``alkera_core.project``."""
    sandbox_dir: Any = None
    """The per-chat scratch dir (``<chat>/sandbox/``, a ``Path``) — where the
    materialize/transform tools write real files the agent can run its own code
    over, and where plan mode writes ``plan.md``. ``None`` outside a chat session
    (the editor ``tool.call`` path / the no-session fallback); a tool that needs to
    materialize must error cleanly when it's ``None``."""
    background: Any = None
    """The chat's ``BackgroundJobRegistry`` — the ``background_status`` /
    ``background_cancel`` tools read + cancel through it. Set for a ROOT session;
    ``None`` for a subagent (the background tools are gated out, app=="background")
    or outside a chat. Typed loosely to keep ``plugin_base`` decoupled from the
    harness layer that defines the registry."""
    abort: Any = None
    """The session's per-turn abort signal (an ``asyncio.Event``), set when the
    turn is cancelled (Ctrl-C). A long-running FOREGROUND tool (``bash``) races its
    work against it so a turn cancel reaps the still-running process group — the
    loopback MCP request itself is NOT cancelled on a turn cancel (stateless server,
    detached dispatch), so this in-parent signal is the reap path. ``None`` outside a
    chat session (the editor ``tool.call`` path) → the tool just runs to completion.
    Typed loosely (``asyncio.Event``) to keep ``plugin_base`` decoupled."""
    tool_scope: ToolScope = None
    """The session's tool restriction, carried so a dispatch made FROM a tool
    (``call_tool``) binds it again: a scoped subagent reaches through the
    indirection exactly what it reaches directly."""
    fence: Any = None
    """The session's bound (a cloud chat's ``SessionFence``), or ``None`` for a
    local session. The shell gate judges every command by where it reads and
    writes through it; the shell runs in its ``working_dir`` with the
    environment it scrubs. Typed loosely to keep ``plugin_base`` decoupled."""
    identity_resolver: Callable[[str], Any] | None = None
    """Answers a session id with the identity (uid, gid) that owns its tree, for
    the files a tool makes there; ``None`` asks the resolver the daemon
    installed for every chat tree. Injected by a test that pins an owner
    without a box."""
    credential: Any = None
    """The chat's credential (``account.binding.ChatCredential``): a local
    chat's bound sign-in profile, or a cloud chat's own token. A tool that
    reaches the cloud for the chat acts as this, never as whichever sign-in is
    current now. ``None`` outside a chat (an editor ``tool.call``)."""

    def dispatch_kwargs(self) -> dict[str, Any]:
        """This context as the keyword arguments of ``ToolRegistry.dispatch`` —
        the ONE spelling of what a nested dispatch carries, so a tool that
        dispatches another (``call_tool``) cannot drop a handle by forgetting
        to name it."""
        return {
            "broker": self.broker,
            "permission_mode": self.permission_mode,
            "permissions": self.permissions,
            "session_id": self.session_id,
            "owner_session_id": self.owner_session_id,
            "spawn": self.spawn,
            "tool_scope": self.tool_scope,
            "decision_sink": self.decision_sink,
            "judge": self.judge,
            "task_goal": self.task_goal,
            "alkera_dir": self.alkera_dir,
            "task_store": self.task_store,
            "sandbox_dir": self.sandbox_dir,
            "background": self.background,
            "abort": self.abort,
            "fence": self.fence,
            "credential": self.credential,
        }

    def resolve_connection(self, handle: str) -> Connection:
        conn = self.registry.connection_for(handle)
        if conn is None:
            # Name the real handles so the agent recovers instead of guessing
            # again (or, when there are none, that there's nothing to query).
            available = sorted(c.handle for c in self.registry.connections())
            hint = (
                f"available connections: {', '.join(available)} "
                "(use sql.connections to inspect them)"
                if available
                else "no data connections are configured — the user must add one"
            )
            raise ToolError(f"unknown connection handle {handle!r}; {hint}")
        return conn

    def capabilities_for(self, handle: str) -> CapabilitySet | None:
        """The live ``CapabilitySet`` for a connection handle (a tool taking a
        ``connection`` arg resolves its own caps through this)."""
        return self.registry.capabilities_for(handle)


#: JSON-Schema keywords Pydantic emits that a provider's tool-schema validator may reject.
#: ``discriminator`` is the dangerous one: it is an OpenAPI keyword, NOT part of JSON Schema
#: draft 2020-12, and Pydantic emits it for every discriminated union (``Field(discriminator=)``).
#: A tool whose schema is rejected does not fail alone — the provider can drop the ENTIRE tool
#: list, which presents as the agent suddenly having no Alkera tools at all. Stripping it is
#: lossless for the model: each union variant still pins its tag as a ``const``, so the variants
#: stay distinguishable, and validation is done by Pydantic against the real model anyway.
_UNSUPPORTED_SCHEMA_KEYS = ("discriminator",)


def _resolve_ref(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """One hop of ``$ref`` into ``$defs``; anything else passes through."""
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return node
    target = defs.get(ref.removeprefix("#/$defs/"))
    return target if isinstance(target, dict) else node


def _tag_values(prop: dict[str, Any]) -> list[Any] | None:
    """The fixed values a property admits (``const`` or ``enum``), else None."""
    if "const" in prop:
        return [prop["const"]]
    enum = prop.get("enum")
    return list(enum) if isinstance(enum, list) else None


def _merge_variant_property(merged: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """One advertised property schema admitting both variants' values.

    Two fixed-value variants (the union's discriminator tags) fold into one
    ``enum`` carrying every tag, declaration order preserved; the first variant
    keeps the metadata (title/description/default), matching the merge's
    first-variant-wins rule everywhere else. Anything not expressible as a tag
    fold becomes an ``anyOf`` of the distinct variants, which is legal at property
    depth; only the ROOT must be an object schema."""
    merged_tags = _tag_values(merged)
    new_tags = _tag_values(new)
    if merged_tags is not None and new_tags is not None:
        folded = {k: v for k, v in merged.items() if k != "const"}
        folded["enum"] = merged_tags + [t for t in new_tags if t not in merged_tags]
        return folded
    pool: list[dict[str, Any]] = list(merged["anyOf"]) if set(merged) == {"anyOf"} else [merged]
    if new not in pool:
        pool.append(new)
    return {"anyOf": pool}


def _flatten_union_root(schema: dict[str, Any]) -> dict[str, Any]:
    """Give a union-rooted schema an OBJECT root.

    A ``RootModel[Union[...]]`` Input renders as a bare ``oneOf`` with no ``type`` and no
    ``properties``. MCP requires a tool's ``inputSchema`` to be an object schema, so a
    consumer that converts tool schemas (opencode does) throws on it — and it does not drop
    just that tool, it drops the WHOLE server's tool list. The agent then reports having only
    its native tools, with nothing wrong in our logs: we served the list correctly and the
    consumer discarded it.

    The variants are merged into one object: every variant's properties, with ``required``
    narrowed to the fields required by EVERY variant (a field only some modes need must not
    read as always-mandatory). A property that differs across variants merges to a schema
    admitting EVERY variant's value. The MCP layer validates a call against this
    ADVERTISED schema before dispatch ever reaches the real ``RootModel`` union, so a
    merged discriminator that kept one variant's ``const`` would make every other mode
    uncallable (the union's own typed errors still cover what the merge cannot express).
    """
    variants = schema.get("oneOf") or schema.get("anyOf")
    if not isinstance(variants, list) or not variants:
        return schema
    raw_defs = schema.get("$defs")
    defs: dict[str, Any] = raw_defs if isinstance(raw_defs, dict) else {}
    resolved = [_resolve_ref(v, defs) for v in variants if isinstance(v, dict)]
    resolved = [v for v in resolved if isinstance(v.get("properties"), dict)]
    if not resolved:
        return schema

    properties: dict[str, Any] = {}
    for variant in resolved:
        for name, prop in variant["properties"].items():
            prior = properties.get(name)
            if prior is None:
                properties[name] = prop
            elif prior != prop:
                properties[name] = _merge_variant_property(prior, prop)

    required_sets = [
        {r for r in variant.get("required", []) if isinstance(r, str)} for variant in resolved
    ]
    required = sorted(set.intersection(*required_sets)) if required_sets else []

    flattened: dict[str, Any] = {
        k: v for k, v in schema.items() if k not in ("oneOf", "anyOf", "$defs")
    }
    flattened["type"] = "object"
    flattened["properties"] = properties
    if required:
        flattened["required"] = required
    # `$defs` is kept only when a surviving property still points into it.
    if defs and "$ref" in json.dumps(properties):
        flattened["$defs"] = defs
    return flattened


def sanitize_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a generated tool input schema safe to put on the wire: an object root, and no
    keywords a provider's schema validator rejects."""

    def _clean(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _clean(v) for k, v in node.items() if k not in _UNSUPPORTED_SCHEMA_KEYS}
        if isinstance(node, list):
            return [_clean(v) for v in node]
        return node

    cleaned: dict[str, Any] = _clean(schema)
    if cleaned.get("type") != "object" or "properties" not in cleaned:
        cleaned = _flatten_union_root(cleaned)
    return cleaned


class Tool(Generic[TIn, TOut], ABC):
    """A strongly-typed agent tool.

    ``Input``/``Output`` are the single source of truth: the JSON-Schema the
    model sees is ``Input.model_json_schema()``, and ``invoke`` validates raw
    args into a typed ``TIn`` at the boundary before dispatching ``run``.
    """

    spec: ClassVar[ToolSpec]
    Input: ClassVar[type[BaseModel]]
    Output: ClassVar[type[BaseModel]]

    @abstractmethod
    async def run(self, args: TIn, ctx: ToolContext) -> TOut: ...

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        """The LLM-facing JSON-Schema — derived, never hand-drifted.

        Deliberately the RAW Pydantic schema, unions and all: it is the most precise
        description of the call, and ``search_tools`` returns it as DATA the model reads.
        Only the MCP wire needs the flattened object form — see
        :func:`sanitize_input_schema`, applied in ``alkera_tool_descriptors``."""
        return cls.Input.model_json_schema()

    @classmethod
    def validate_args(cls, raw: dict[str, Any]) -> BaseModel:
        """Parse raw args into the typed ``Input`` model (raises
        ``ValidationError`` at the boundary, not a deep ``KeyError``)."""
        return cls.Input.model_validate(raw)

    async def invoke(self, raw: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Validate → dispatch → serialize. The framework's four-step
        boundary."""
        args = type(self).validate_args(raw)
        out = await self.run(args, ctx)  # type: ignore[arg-type]
        return serialize_tool_result(out)


#: The runtime bindings a team record's connection carries: the record's id
#: and the name the server gave it (its local handle may carry a suffix).
TEAM_RECORD_ID: Final = "team_record_id"
TEAM_RECORD_HANDLE: Final = "team_record_handle"


class ToolRegistry:
    """One per ``HarnessRuntime``. The daemon-side tool surface.

    The hot prefix (first-class tools + ``search_tools`` + ``call_tool``) is
    composed ONCE per session and never mutated → the prompt cache is preserved
    by construction. The long tail is discovered via ``search`` and invoked via
    ``dispatch`` (the ``call_tool`` core) — no provider-native Tool Search, no
    ``defer_loading``, no messages-region enable/defer path.
    """

    def __init__(
        self,
        blobs: BlobStore,
        *,
        cap_token_mint: Callable[..., Awaitable[Any]] | None = None,
        context_store: Any = None,
        lineage_store: Any = None,
        cost_ledger: Any = None,
        decision_sink: Any = None,
        skills: Any = None,
        embedder: Any = None,
        agents: Any = None,
        plugin_snapshot: Any = None,
        lineage_jobs_snapshot: Callable[[], Sequence[Any]] | None = None,
        connections_provider: Callable[[], list[Connection]] | None = None,
        capabilities_resolver: Callable[[Connection], CapabilitySet | None] | None = None,
        knowledge_reader_resolver: Callable[[Connection], Any] | None = None,
        subagents_enabled: bool = True,
    ) -> None:
        self._blobs = blobs
        #: The runtime's environment service, handed over when a session starts
        #: (it waits out a restore and records which spec a chat captured).
        self.environment: EnvironmentService | None = None
        #: How a cloud chat's sandbox launch is made, provided by the shell tools
        #: for the runtime's environment service (which cannot import them).
        self.chat_launch_factory: ChatRunnerFactory | None = None
        #: The runtime's notebook service (``alkera_cli.notebooks.tools.NotebookService``):
        #: the workspace engines and digest cursors the notebook tools reach through.
        #: ``None`` where the runtime serves no notebooks. Typed loosely to keep
        #: ``plugin_base`` decoupled from the notebook layer.
        self.notebooks: Any = None
        self._builtin_skills: list[Any] = []
        # A deployment can withhold delegation entirely. False means the
        # agent-spawning tools are never REGISTERED — on any backend, a denied
        # permission still leaves a tool advertised, so the only way to stop the
        # model from reaching for one is to keep it out of the catalog — and a
        # call that arrives anyway (a resumed session, a replayed transcript) is
        # refused by name in ``dispatch``.
        self._subagents_enabled = subagents_enabled
        self._cap_token_mint = cap_token_mint
        self._context_store = context_store
        self._lineage_store = lineage_store
        self._cost_ledger = cost_ledger
        self._decision_sink = decision_sink
        # skills / agents / plugin_snapshot accept EITHER a static list OR a zero-arg
        # callable (a LIVE provider re-read on each access) — same pattern as
        # ``lineage_jobs_snapshot``. The runtime passes callables so a connection /
        # plugin added AFTER this registry was built (mid-session, or in another
        # process via the shared connections.json) is reflected without rebuilding the
        # session; tests pass plain lists. The property getters normalize both.
        self._skills = skills
        # The context layer's EmbeddingFunction (loosely typed to keep plugin_base
        # decoupled). When present, ``search`` is hybrid (BM25 ⊕ dense); when None
        # or unavailable, it degrades to the BM25 floor.
        self._embedder = embedder
        self._agents = agents
        self._plugin_snapshot = plugin_snapshot
        self._lineage_jobs_snapshot = lineage_jobs_snapshot
        # LIVE connection resolution (or None in tests): a connection added after this
        # registry was built is resolved from the persisted ADDED allow-list on demand,
        # so the open chat can query it without a restart. The resolver only ever
        # returns connections in the added allow-list — a detected-but-not-added
        # connection stays unreachable (the prod-by-accident guard holds).
        self._connections_provider = connections_provider
        self._capabilities_resolver = capabilities_resolver
        # Resolves a connection to the provider that reads its documents, or None for a
        # plugin that reads none. Typed loosely for the reason the others are: the
        # knowledge layer imports this module, so naming its Protocol here would close
        # the loop.
        self._knowledge_reader_resolver = knowledge_reader_resolver
        self._tools: dict[str, type[Tool[Any, Any]]] = {}
        self._tool_connection: dict[str, str] = {}
        self._connections: dict[str, Connection] = {}
        self._capabilities: dict[str, CapabilitySet] = {}
        self._pinned: set[str] = set()
        self._index: Bm25Index | None = None
        self._dense: dict[str, list[float]] | None = None
        self._index_dirty = True
        # A view made by ``restricted``: the names it withholds, and the registry
        # whose search index it ranks over. Empty / None on a registry built
        # directly.
        self._withheld: frozenset[str] = frozenset()
        self._catalog_of: ToolRegistry | None = None
        # A view's connection scope: the team-record ids this session may see,
        # or None for every connection the build carries (see ``restricted``).
        self._connection_ids: frozenset[str] | None = None
        # The principal a scoped view files and reads unattributed knowledge
        # for; "" on an unscoped registry, and on a scoped view whose session
        # named nobody, which then reads no note at all.
        self._knowledge_owner: str = ""

    @property
    def context_store(self) -> Any:
        """The project's KB store (``ContextStore``), or ``None``. Typed loosely to
        keep ``plugin_base`` decoupled from the context engine.

        On a scoped view it is the store as THIS session may read it: the cards
        of the connections in scope and the notes of the session's owner, and
        nothing another chat on the same machine holds. A store that cannot be
        scoped is no knowledge base for a scoped view, rather than the whole one."""
        return self._as_this_view(self._context_store)

    @property
    def knowledge_owner(self) -> str:
        """The principal this view files unattributed knowledge under."""
        return self._knowledge_owner

    @property
    def lineage_store(self) -> Any:
        """The project's lineage fact store (``LineageFactStore``), or ``None``.

        On a scoped view it is the store as THIS session may read it, the way
        ``context_store`` is: the facts of the connections in scope (a
        per-person connection's only as its owner's credential saw them) and no
        write. A store that cannot be scoped is no lineage for a scoped view."""
        return self._as_this_view(self._lineage_store)

    def _as_this_view(self, store: Any) -> Any:
        """``store`` as this view reads it. An unscoped view reads the whole
        store; a scoped one reads ``store.scoped(connections, owner=...)`` over
        the connections in scope and this view's owner, and a store with no
        such method is no store at all to a scoped view, never the whole one."""
        if store is None or self._connection_ids is None:
            return store
        scoped = getattr(store, "scoped", None)
        if scoped is None:
            return None
        return scoped(self.connections(), owner=self._knowledge_owner)

    @property
    def lineage_jobs_snapshot(self) -> Callable[[], Sequence[Any]] | None:
        """Returns the live scheduler job list (``Scheduler.list_jobs()``), or ``None``
        when no scheduler is bound (tests / non-daemon paths). The lineage tools call it
        to detect an in-flight refresh and briefly wait it out before querying a possibly
        stale graph. Loosely typed (``Sequence[Any]``) to keep ``plugin_base`` decoupled
        from ``SchedulerStore`` / ``ScheduledJob``.

        The scheduler is the machine's, and on a box it runs every org's refreshes. A
        scoped view reads only the jobs of connections in its scope; the column drain
        names no connection and is the machine's work, so a scoped view does not see
        it either."""
        provider = self._lineage_jobs_snapshot
        if provider is None or self._connection_ids is None:
            return provider
        handles = {c.handle for c in self.connections()}

        def scoped() -> Sequence[Any]:
            jobs = provider()
            return [
                j for j in jobs if (getattr(j, "payload", None) or {}).get("connection") in handles
            ]

        return scoped

    @property
    def plugin_snapshot(self) -> list[Any]:
        """All plugins + their connections (``PluginInfo``) for the ``list_plugins``
        discovery tool — active/inactive, descriptions, URN formats, connections.
        Read LIVE when a provider was given, so a connection/plugin added after this
        registry was built (mid-session, or in another process) shows up."""
        snap = self._plugin_snapshot() if callable(self._plugin_snapshot) else self._plugin_snapshot
        plugins = list(snap) if snap else []
        if self._connection_ids is None:
            return plugins
        return [self._narrow_plugin_info(info) for info in plugins]

    def _narrow_plugin_info(self, info: Any) -> Any:
        """One plugin's listing as this scoped view tells it: only the
        connections this session may see are named, and the plugin reads
        ``active`` when this session holds one of its connections — or when it
        is active with no connection at all (the workspace turned it on), since
        a plugin the build activated only for another chat's connection is not
        one this chat can use. A detected row is named only when it was learned
        through a connection this session holds (an in-scope AWS account's
        bucket); anything else detected on a shared machine may be another
        org's discovery."""
        admitted = {(c.plugin, c.handle) for c in self.connections()}
        carried = {(c.plugin, c.handle) for c in self._every_connection()}
        connections = [
            c
            for c in info.connections
            if (c.plugin, c.handle) in admitted or (not c.added and c.derived_from in admitted)
        ]
        has_admitted = any((c.plugin, c.handle) in admitted for c in connections)
        has_carried = any(plugin == info.name for plugin, _handle in carried)
        return info.model_copy(
            update={
                "connections": connections,
                "active": bool(info.active and (has_admitted or not has_carried)),
            }
        )

    @property
    def cost_ledger(self) -> Any:
        """The project's ``CostLedger``, or ``None`` (metering disabled).

        On a scoped view it is the ledger as this session's owner meters against
        it: their own day and week totals, never the machine's sum over every
        chat it serves."""
        ledger = self._cost_ledger
        if ledger is None or self._connection_ids is None:
            return ledger
        for_principal = getattr(ledger, "for_principal", None)
        return None if for_principal is None else for_principal(self._knowledge_owner)

    @property
    def skills(self) -> list[Any]:
        """The active plugins' ``SkillDef``s — markdown guidance the agent can load
        on demand via the ``use_skill`` tool. Empty when no plugin ships one.
        Read LIVE when a provider was given, so a plugin activated mid-session
        contributes its skills without a chat restart."""
        skills = self._skills() if callable(self._skills) else self._skills
        return [*(skills or []), *self._builtin_skills]

    def add_skill(self, skill: Any) -> None:
        """Serve a built-in ``SkillDef`` beside the plugins' (the notebook tools
        bring the ``notebooks`` skill). A skill of the same name is replaced."""
        self._builtin_skills = [s for s in self._builtin_skills if s.name != skill.name]
        self._builtin_skills.append(skill)

    @property
    def agents(self) -> list[AgentDefinition]:
        """The spawnable subagent definitions (built-ins + ``.alkera/agents/*.md`` +
        programmatic) — backs the ``list_agent_types`` tool. Read LIVE when a provider
        was given, so an agent file added mid-session is picked up (mirrors ``skills``)."""
        agents = self._agents() if callable(self._agents) else self._agents
        return list(agents) if agents else []

    # --- registration --------------------------------------------------

    @property
    def subagents_enabled(self) -> bool:
        """Whether this registry carries the agent-spawning tools at all."""
        return self._subagents_enabled

    def register(self, tool: type[Tool[Any, Any]], *, connection: Connection | None = None) -> None:
        name = tool.spec.name
        if not self._subagents_enabled and tool.spec.app == SUBAGENT_TOOL_APP:
            # Registration is the gate, so a subagent tool added later is withheld
            # by the same switch without anyone remembering to list its name.
            return
        if name in self._tools:
            raise ValueError(f"tool {name!r} already registered")
        self._tools[name] = tool
        if connection is not None:
            self._tool_connection[name] = connection.handle
        self._index_dirty = True  # catalog changed → rebuild on next search

    def register_connection(
        self, conn: Connection, *, capabilities: CapabilitySet | None = None
    ) -> None:
        self._connections[conn.handle] = conn
        if capabilities is not None:
            self._capabilities[conn.handle] = capabilities

    def pin(self, name: str) -> None:
        """Pin a normally-deferred tool into the always-on hot prefix
        (a user pin). Must be called before the session composes its
        prefix — it does not mutate a live array."""
        self._pinned.add(name)

    def restricted(
        self,
        withheld: frozenset[str],
        *,
        connection_ids: frozenset[str] | None = None,
        knowledge_owner: str | None = None,
    ) -> ToolRegistry:
        """This registry as one session sees it, without the tools in ``withheld``
        and — when ``connection_ids`` is given — with only the team connections
        whose record id is in it, and the knowledge store read as that session.

        A runtime builds its registry once for the project and every chat on it
        shares that build, so the build itself must not encode anything that is
        one chat's — an org's web-tools toggle, on a box that serves many orgs.
        Each session binds to a view instead: the same stores, providers,
        connections and search index, and a catalog missing the withheld names.
        A withheld tool is absent from the hot prefix, from ``tool_for`` and from
        a search, and a dispatch by name is refused with the reason rather than
        reported unknown, so a model that learned the name elsewhere is told it
        is policy and not a typo.

        ``connection_ids`` is the connection scope. On a box that serves chats
        of several orgs the workspace's team store is the UNION of what every
        held chat's owner may use, and the build carries that union; a chat's
        view admits only the team records the server answered for that chat,
        so ``connections()``, ``connection_for()``, ``capabilities_for()``,
        ``knowledge_reader_for()`` and ``plugin_snapshot`` — everything a tool
        asks "which connections exist" through — answer that chat's set and
        nothing of another org's. A connection that is nobody's team record
        (the workspace's own allow-list) is not in scope either: on a shared
        machine the only connections a chat has are the ones resolved for it.
        ``None`` is the unscoped view a person's own daemon uses, where every
        connection in the store is theirs; an empty set sees no connection.

        A scoped view reads the knowledge store the same way (``context_store``):
        the cards of the connections in scope, and the notes ``knowledge_owner``
        — the chat owner's user id — wrote. On a shared machine the store holds
        every org's cards and every person's notes, and the sql surface being
        scoped while ``context_search`` read the whole store is exactly how one
        org's schema reached another org's chat. A scoped view with no owner
        reads no note; a view of a view keeps the owner it was given.

        Withholding nothing and scoping nothing is this registry itself.
        """
        if not withheld and connection_ids is None:
            return self
        view = copy.copy(self)
        view._tools = {name: tool for name, tool in self._tools.items() if name not in withheld}
        view._withheld = frozenset(withheld) | self._withheld
        if connection_ids is not None:
            # A view of a view narrows: it never widens what the outer view admits.
            inner = self._connection_ids
            view._connection_ids = (
                frozenset(connection_ids) if inner is None else frozenset(connection_ids) & inner
            )
        if knowledge_owner is not None:
            # A view of a view keeps the owner it was given; naming a different
            # one reads as nobody's rather than as the other person's.
            outer = self._knowledge_owner
            view._knowledge_owner = knowledge_owner if outer in ("", knowledge_owner) else ""
        # One search index per project: the view ranks over the shared corpus
        # and drops what it does not carry, rather than embedding it again.
        view._catalog_of = self if self._catalog_of is None else self._catalog_of
        return view

    @property
    def withheld(self) -> frozenset[str]:
        """The tool names this view keeps out of its catalog."""
        return self._withheld

    @property
    def connection_ids(self) -> frozenset[str] | None:
        """The team-record ids this view admits, or ``None`` for every connection."""
        return self._connection_ids

    def _admits(self, conn: Connection) -> bool:
        """Whether ``conn`` is in this view's connection scope.

        A scoped view is a cloud box's: it admits the records the server answered
        for the chat or workspace, which are its owner's (sharing a workspace
        shares every connection its owner may use). A ``per_user`` one among them
        runs on the OWNER's own grant, leased for that chat or workspace alone and
        cached under it (``relay_client``), never from the machine's member slot."""
        if self._connection_ids is None:
            return True
        record_id = conn._runtime_bindings.get("team_record_id", "")
        return bool(record_id) and record_id in self._connection_ids

    # --- lookup --------------------------------------------------------

    def _every_connection(self) -> list[Connection]:
        """Every connection the build carries, before this view's scope."""
        if self._connections_provider is not None:
            return list(self._connections_provider())
        return list(self._connections.values())

    def connection_for(self, handle: str) -> Connection | None:
        # When a LIVE provider is wired it is the SOURCE OF TRUTH: only a currently-added
        # connection resolves — one added after this registry was built does, and one
        # removed since stops. The provider reads the persisted ADDED allow-list, so a
        # detected-but-not-added connection never resolves (the prod-by-accident guard).
        # Without a provider (tests / non-runtime paths) fall back to the build-time set.
        # A view's scope applies last: a handle the build knows but this session
        # was not given resolves to nothing, exactly like a handle that does not exist.
        conn = next((c for c in self._every_connection() if c.handle == handle), None)
        if conn is None or not self._admits(conn):
            return None
        return conn

    def connections(self) -> list[Connection]:
        return [c for c in self._every_connection() if self._admits(c)]

    def connection_by_record(self, record_id: str) -> Connection | None:
        """The connection the team record ``record_id`` materializes as, when
        this view admits it. Whatever local handle the record took on this
        machine (a collision suffixes it), the record is the identity."""
        if not record_id:
            return None
        return next(
            (c for c in self.connections() if c._runtime_bindings.get(TEAM_RECORD_ID) == record_id),
            None,
        )

    def connection_named(self, name: str) -> Connection | None:
        """The one admitted connection ``name`` means, as a person or a cell
        names it: the name the server gave a team record (before any local
        suffix), else a connection's own handle. ``None`` for no match, and
        for more than one, which is never guessed between."""
        matches = [
            c
            for c in self.connections()
            if c._runtime_bindings.get(TEAM_RECORD_HANDLE, c.handle) == name
        ]
        return matches[0] if len(matches) == 1 else None

    def knowledge_reader_for(self, conn: Connection) -> Any:
        """The provider that reads this connection's documents, or None when its plugin
        reads none, when the connection is outside this view's scope, and when no
        resolver is wired at all. Asked per call, so a plugin disabled mid-session
        stops answering for its connections."""
        if self._knowledge_reader_resolver is None or not self._admits(conn):
            return None
        return self._knowledge_reader_resolver(conn)

    def capabilities_for(self, handle: str) -> CapabilitySet | None:
        conn = self.connection_for(handle)
        if conn is None:
            # With a live provider, an unresolved handle (un-added or removed) has no
            # capabilities; nor does one outside this view's scope. Without either,
            # fall back to the build-time cache.
            if self._connections_provider is not None or self._connection_ids is not None:
                return None
            return self._capabilities.get(handle)
        # A live resolver is the SOURCE OF TRUTH — compute caps for the CURRENTLY-resolved
        # connection, NEVER the build-time cache: mid-session the handle may map to a
        # different connection/plugin than when this registry was built (removed + re-added),
        # and a stale cached CapabilitySet would route e.g. call_integration_sdk to the wrong
        # vendor factory. Only when no resolver is wired (tests) fall back to the cache.
        if self._capabilities_resolver is not None:
            return self._capabilities_resolver(conn)
        return self._capabilities.get(handle)

    def all_specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    def tool_for(self, name: str) -> type[Tool[Any, Any]] | None:
        return self._tools.get(name)

    def input_schema_for(self, name: str) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool {name!r}")
        return tool.input_schema()

    # --- the fixed hot prefix ------------------------------------------

    def hot_prefix(self) -> list[ToolSpec]:
        """The FIXED hot set — every tool with ``spec.hot`` (the meta-tools +
        first-class tools) plus any user-pinned tool. Deterministic sort by
        name so the serialized array is byte-stable across the session."""
        specs = [t.spec for t in self._tools.values() if t.spec.hot or t.spec.name in self._pinned]
        return sorted(specs, key=lambda s: s.name)

    # --- search (the ``search_tools`` core) ----------------------------

    def _searchable(self) -> list[ToolSpec]:
        """Every tool a search can name — the long tail AND the hot prefix, so a
        query for an area (``lineage``) lists that area's tools even when they are
        already loaded. Only the discovery tools themselves are left out: a search
        that answers ``search_tools`` tells the caller nothing."""
        return [t.spec for t in self._tools.values() if t.spec.name not in _NOT_SEARCHABLE]

    def _ensure_index(self) -> None:
        if self._catalog_of is not None:
            # A view ranks over the registry it was made from; its own catalog
            # only decides which of the ranked names it may answer with.
            self._catalog_of._ensure_index()
            self._index, self._dense = self._catalog_of._index, self._catalog_of._dense
            return
        if self._index is not None and not self._index_dirty:
            return
        documents = {
            spec.name: self._index_doc(spec, self.input_schema_for(spec.name))
            for spec in self._searchable()
        }
        self._index = Bm25Index(documents)
        self._dense = self._embed_corpus(documents)
        self._index_dirty = False

    def _embed_corpus(self, documents: dict[str, str]) -> dict[str, list[float]] | None:
        """Embed the searchable-tool corpus once per catalog change, reusing the
        SAME ``_index_doc`` text BM25 indexes (no second text extraction). Returns
        None — the BM25 floor — when no embedder is wired or it's unavailable (e.g.
        the local model isn't bundled yet), so search degrades gracefully."""
        if self._embedder is None or not documents:
            return None
        try:
            names = list(documents)
            vectors = self._embedder.embed([documents[name] for name in names])
            return {name: [float(x) for x in vec] for name, vec in zip(names, vectors, strict=True)}
        except Exception:
            logger.debug("tool search dense index unavailable; falling back to BM25", exc_info=True)
            return None

    @staticmethod
    def _index_doc(spec: ToolSpec, schema: dict[str, Any]) -> str:
        """The indexed text for a tool: name + description + app + the param
        names from its JSON-Schema (PLUGINS / the search-corpus correction)."""
        params = _collect_param_names(schema)
        return " ".join([spec.name, spec.title, spec.description, spec.app or "", *params])

    async def search(self, query: str, *, app: str | None = None, k: int = 8) -> list[ToolSpec]:
        """Hybrid (BM25 ⊕ dense) over the searchable catalog with a structural
        ``app`` pre-filter, falling back to the BM25 floor when no embedder is
        wired. Rebuilds the index only when the catalog
        changed (never per query)."""
        self._ensure_index()
        assert self._index is not None
        # Over-fetch then app-filter so the filter doesn't starve k; hybrid scores
        # the whole corpus, so the over-fetch only bounds the BM25 candidate pool.
        over = max(k * 4, 40) if self._dense else (k * 4 if app else k)
        bm25_scored = self._index.search(query, k=over)
        ranked = (
            self._fuse(query, bm25_scored) if self._dense else [name for name, _ in bm25_scored]
        )
        # The index may be a wider registry's than this view's catalog (see
        # ``restricted``): a name this view withholds is never an answer.
        ranked = [name for name in ranked if name in self._tools]
        # A one-word query that names an area (``postgres``, ``lineage``) lists that
        # area's tools before the ones that only mention it; the relevance order
        # holds within each group. A sentence is left to the ranking, where a word
        # like ``sql`` in it would otherwise lift a whole family over the answer.
        terms = set(tokenize(query))
        if len(terms) == 1:
            ranked = sorted(ranked, key=lambda name: _naming_rank(self._tools[name].spec, terms))
        out: list[ToolSpec] = []
        for name in ranked:
            spec = self._tools[name].spec
            if app is not None and spec.app != app:
                continue
            out.append(spec)
            if len(out) >= k:
                break
        return out

    def _fuse(self, query: str, bm25_scored: list[tuple[str, float]]) -> list[str]:
        """Score-aware hybrid: min-max-fuse the BM25 scores with dense cosine over
        the cached corpus vectors (``min_max_fusion``). BM25 is weighted higher
        (``_FUSION_BM25_WEIGHT``) so a confident lexical hit is never displaced —
        yet a strong dense-only hit (the pure-semantic paraphrase BM25 can't get)
        still surfaces, because the fusion is query-adaptive (a blind ranker's
        normalized contribution is small relative to the confident peak). Any
        query-embed failure degrades to the BM25 ranking."""
        from alkera_cli.ranking import dense_similarity_floor, min_max_fusion

        assert self._dense is not None
        try:
            qvec = [float(x) for x in self._embedder.embed([query])[0]]
        except Exception:
            return [name for name, _ in bm25_scored]
        bm25 = dict(bm25_scored)
        dense = {name: _cosine(qvec, vec) for name, vec in self._dense.items()}
        fused = min_max_fusion(
            [bm25, dense], weights=[_FUSION_BM25_WEIGHT, 1.0 - _FUSION_BM25_WEIGHT]
        )
        # Every tool has a dense score, so without a floor every query answered the
        # whole catalog in a new order. Cut after fusion: a keyword hit always
        # stays, a meaning-only hit stays when it is close enough to be about the
        # query, and the order among the survivors is the fused order.
        floor = dense_similarity_floor()
        return [name for name, _score in fused if name in bm25 or dense[name] >= floor]

    # --- dispatch (the ``call_tool`` core) -----------------------------

    def build_context(
        self,
        *,
        connection: Connection | None = None,
        broker: Any = None,
        permission_mode: str = "default",
        permissions: Any = None,
        session_id: str = "",
        spawn: Callable[..., Awaitable[SubagentRunResult]] | None = None,
        decision_sink: Any = None,
        judge: Any = None,
        task_goal: str = "",
        alkera_dir: Any = None,
        task_store: Any = None,
        sandbox_dir: Any = None,
        background: Any = None,
        abort: Any = None,
        tool_scope: ToolScope = None,
        fence: Any = None,
        owner_session_id: str = "",
        credential: Any = None,
    ) -> ToolContext:
        # A tool names blobs as its chat (the root chat, for a subagent), so a
        # hash another chat produced reads as one that does not exist.
        chat_key = owner_session_id or session_id
        return ToolContext(
            registry=self,
            blobs=self._blobs.for_chat(chat_key) if chat_key else self._blobs,
            connection=connection,
            broker=broker,
            cap_token_mint=self._cap_token_mint,
            permission_mode=permission_mode,
            permissions=permissions,
            session_id=session_id,
            owner_session_id=owner_session_id or session_id,
            spawn=spawn,
            cost_ledger=self.cost_ledger,
            # A per-chat sink (from the session binding) overrides the registry's
            # project-level fallback so in-chat decisions log to the chat's own file.
            decision_sink=decision_sink if decision_sink is not None else self._decision_sink,
            judge=judge,
            task_goal=task_goal,
            alkera_dir=alkera_dir,
            task_store=task_store,
            sandbox_dir=sandbox_dir,
            background=background,
            abort=abort,
            tool_scope=tool_scope,
            fence=fence,
            credential=credential,
        )

    async def dispatch(
        self,
        name: str,
        raw_args: dict[str, Any],
        *,
        broker: Any = None,
        permission_mode: str = "default",
        permissions: Any = None,
        session_id: str = "",
        spawn: Callable[..., Awaitable[SubagentRunResult]] | None = None,
        tool_scope: ToolScope = None,
        decision_sink: Any = None,
        judge: Any = None,
        task_goal: str = "",
        alkera_dir: Any = None,
        task_store: Any = None,
        sandbox_dir: Any = None,
        background: Any = None,
        abort: Any = None,
        fence: Any = None,
        owner_session_id: str = "",
        credential: Any = None,
    ) -> dict[str, Any]:
        """Look up → scope-check → effect-gate → validate → run → serialize. The
        ONLY way a non-hot tool is invoked. A bad name / out-of-scope tool /
        validation error / capability miss surfaces as a clean model-visible
        error result, never a crash."""
        if not self._subagents_enabled and name in SUBAGENT_TOOL_NAMES:
            # Ahead of the lookup: the tool was never registered, and "unknown
            # tool" would send the model hunting for the right name instead of
            # telling it delegation is off here.
            return tool_error_result(SUBAGENT_DISABLED_REASON, tool=name)
        if name in self._withheld:
            # Also ahead of the lookup: the tool exists in the project's registry
            # and this session was not given it (an org toggle, a deployment
            # switch). "Unknown" would read as a misspelling to the model.
            return tool_error_result(f"tool {name!r} is not enabled for this chat", tool=name)
        tool_cls = self._tools.get(name)
        if tool_cls is None:
            return tool_error_result(f"unknown tool {name!r}", tool=name)
        # Defense-in-depth: a subagent's tool_scope hides out-of-scope tools from
        # the list AND refuses them here (the list is advisory; this is binding).
        if not tool_in_scope(tool_cls.spec, tool_scope):
            return tool_error_result(f"tool {name!r} is not available to this agent", tool=name)
        # Recursion-off backstop: the agent-spawning tools (spawn_agent /
        # list_agent_types, app=="agent") are never reachable from a child — a child
        # has no spawn wiring (``spawn is None``). They're already withheld from the
        # child's advertised descriptors; this refuses an out-of-band dispatch too.
        if tool_cls.spec.app == "agent" and spawn is None:
            return tool_error_result(f"tool {name!r} is not available to subagents", tool=name)
        # The task/TODO tools (app=="tasks") are root-only too: a child has no spawn
        # wiring. They're already withheld from a child's advertised descriptors
        # (allow_task_tools); this refuses an out-of-band dispatch as the binding
        # backstop, exactly like the agent-spawning tools above.
        if tool_cls.spec.app == "tasks" and spawn is None:
            return tool_error_result(f"tool {name!r} is not available to subagents", tool=name)
        # The background-management tools (app=="background") are root-only as well:
        # a subagent can't manage background jobs (it has no spawn wiring). Withheld
        # from a child's advertised set (allow_background_tools); this is the binding
        # backstop against an out-of-band dispatch.
        if tool_cls.spec.app == "background" and spawn is None:
            return tool_error_result(f"tool {name!r} is not available to subagents", tool=name)
        # A bounded (cloud) session is not offered the tools that run code outside
        # the fence; this is the binding backstop against reaching one by name.
        if fence is not None and name in FENCED_CODE_TOOLS:
            return tool_error_result(f"tool {name!r} is {FENCED_CODE_TOOL_REASON}", tool=name)
        ctx = self.build_context(
            broker=broker,
            permission_mode=permission_mode,
            permissions=permissions,
            session_id=session_id,
            spawn=spawn,
            decision_sink=decision_sink,
            judge=judge,
            task_goal=task_goal,
            alkera_dir=alkera_dir,
            task_store=task_store,
            sandbox_dir=sandbox_dir,
            background=background,
            abort=abort,
            tool_scope=tool_scope,
            fence=fence,
            owner_session_id=owner_session_id,
            credential=credential,
        )
        try:
            await self._gate_effect(tool_cls.spec, ctx)
            result = await tool_cls().invoke(raw_args, ctx)
            if not tool_cls.spec.deliver_whole:
                result = await asyncio.to_thread(deliver_generic, ctx.blobs, name, result)
            # A file this call names may have been changed on the web while the
            # agent was changing it; the agent is told where its version went.
            return with_conflict_notices(result, raw_args, sandbox_dir=sandbox_dir)
        except ValidationError as exc:
            return tool_error_result(
                describe_validation_error(name, exc, tool_cls.Input), tool=name
            )
        except ToolError as exc:
            return tool_error_result(str(exc), tool=name, classification=classify_tool_failure(exc))
        except Exception as exc:
            # The binding half of "never a crash": a tool that leaks a raw exception must not
            # kill the transport and the whole turn. The traceback goes to the log; the model
            # gets a shaped failure. CancelledError is a BaseException and passes through
            # untouched, so a turn-cancel still cancels the turn.
            logger.exception("tool %s leaked an unhandled exception", name)
            kind = classify_tool_failure(exc)
            return tool_error_result(f"tool {name!r} failed: {exc}", tool=name, classification=kind)

    async def _gate_effect(self, spec: ToolSpec, ctx: ToolContext) -> None:
        """The pre-gate every dispatched tool passes.

        This is a PRECONDITION check, not the decision. A tool whose effect hint is
        not READ cannot run without a permission broker on the context, so a session
        that never wired one can't reach a mutating or outbound tool at all. The
        decision itself — the descriptor, the mode policy, the auto-mode judge, the
        human prompt, the cap token and the audit record — is built inside each such
        tool's own body, because the harness auto-allows our MCP tools at its own
        layer and this is the last place before the work happens.

        The hint alone is NOT a gate: a READ-hinted tool that can still reach
        outward (``bash``) gates in its body too. Adding a non-READ tool without an
        in-body gate leaves it governed by nothing but this broker check."""
        if spec.effect_hint == Effect.READ:
            return
        if ctx.broker is None:
            raise ToolError(f"{spec.name}: write-effect tools require a permission broker")


#: The discovery tools: never a search result, since naming them answers nothing.
_NOT_SEARCHABLE = frozenset({"search_tools", "call_tool"})


def _naming_rank(spec: ToolSpec, terms: set[str]) -> int:
    """``0`` when a query term names the tool's area — its app or the head of its
    name (``postgres`` → ``postgres.kill_query``, ``lineage`` → ``lineage_find``) —
    and ``1`` otherwise. A stable sort on it keeps the relevance order within each
    group, so a word that only appears later in a name (``table`` in
    ``redshift.table_skew``) earns no lift."""
    name_tokens = tokenize(spec.name)
    head = {name_tokens[0]} if name_tokens else set()
    return 0 if terms & (head | set(tokenize(spec.app or ""))) else 1


_FUSION_BM25_WEIGHT = 0.6
"""BM25's share of the score-aware hybrid (dense gets ``1 - this``). Tuned on the
4a eval: at 0.6 the hybrid strictly improves recall@5 over the BM25 floor (the
pure-semantic litmus flips miss→hit) with no confident lexical hit displaced."""


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _collect_param_names(schema: dict[str, Any]) -> list[str]:
    """Recursively collect property names from a JSON-Schema (incl. ``$defs``
    and ``oneOf``/``anyOf`` variants) so the search corpus includes a tool's
    parameter names."""
    names: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.extend(str(key) for key in props)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(schema)
    return names


__all__ = [
    "TOOL_FAILURE_CLASSIFIERS",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolScope",
    "ToolSpec",
    "classify_tool_failure",
    "sanitize_input_schema",
    "tool_in_scope",
]
