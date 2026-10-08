"""The presenter registry: what each kind of permission ask reads as.

This module is the single source of truth for the words and registers of a
permission prompt. It is plain data -- no surface-specific code -- and it is
exported, as data, into ``@alkera/chat-model`` (``make gen-tool-manifest``), so
the web card and the Slack card are both driven by these entries. A new kind of
ask earns its detail on every surface by ONE :func:`register` call here.

Three layers, applied in this order of specificity:

* ``mechanism`` -- a harness gate keyed by its ``permission_kind`` rather than a
  tool call (``external_directory``, ``doom_loop``). Overrides the question.
* ``capability`` -- the classifier's lane (``sql``, ``knowledge``, ...). Words
  the question by effect / operation / connection, sets the syntax, and names
  what a standing grant covers. ``*`` is the entry for a capability nobody has
  registered: a control-plane action on some connection.
* ``canonical`` -- the harness-agnostic class (``shell``, ``edit``, ...). The
  base every ask has: its fallback question, its subject register, its waiting
  line. ``other`` is the generic fallback: an unknown tool still gets its name
  and its whole input shown, never a bare tag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from alkera_core.permission_presentation.model import SubjectFormat

Layer = Literal["canonical", "capability", "mechanism"]

#: The two knowledge bases, by their product names.
TEAM_KNOWLEDGE_BASE = "Team Knowledge Base"
PROJECT_KNOWLEDGE_BASE = "Project Knowledge Base"

#: The question for an action the classifier found reaching the database
#: server's own host. It outranks every lane: the subject under it reads as data
#: work, so the heading says what it really does.
EXEC_TITLE = "Allow this to run a program or use files on the database server?"

#: The option label the shell sends when a standing grant would record the
#: exact command line (a destructive command), never its verb.
EXACT_ALWAYS_LABEL = "Always allow this exact command"

#: How a settled ask reads, by the option it was settled on: the line a card
#: that has been answered shows in place of its buttons.
DECISION_OUTCOMES: dict[str, str] = {
    "allow_once": "Allowed once",
    "allow_always": "Always allowed",
    "reject_once": "Rejected",
    "reject_always": "Always rejected",
    "cancelled": "Cancelled",
}

#: Where a subject is recovered from the gated call's input when the ask named
#: none, after the presenters' own ``input_keys``: the command line, else the
#: path a write names, else the address a fetch names.
SUBJECT_INPUT_KEYS: tuple[str, ...] = ("command", "path", "filePath", "file_path", "file", "url")


#: Why an ask needs approval, in plain words, by the effect the classifier
#: found. Said once on a card that lists several things to run.
EFFECT_REASONS: dict[str, str] = {
    "read": "Only reads files or data",
    "write": "May change files or data",
    "destroy": "May delete files or data",
    "egress": "May send or fetch data over the network",
    "exec": "May run programs on the database server",
    "memory": "May change what the agent remembers",
}

#: The reason for an operation the effect words would misdescribe: stopping a
#: kernel is classed as a destroy when it ends someone else's run, and nothing
#: on disk is deleted.
OPERATION_REASONS: dict[str, str] = {
    "notebook_kernel": "Stops code running in the notebook",
    "notebook_install": "Downloads packages from the internet",
}

#: Said in the reason's place when the classifier could not tell what the
#: action does (it counts such an action as a write).
UNSURE_REASON = "Can't tell if it changes anything"

#: How much of each cell's code a notebook ask shows: enough lines to
#: recognise the cell, each cut at this many characters.
NOTEBOOK_PREVIEW_LINES = 2
NOTEBOOK_PREVIEW_WIDTH = 80


@dataclass(frozen=True, slots=True)
class TitleRule:
    """One way to word the question. The first rule whose conditions all hold
    is the question; a presenter none of whose rules hold defers to the next
    layer down.

    ``{tool}`` and ``{connection}`` in ``text`` are filled from the ask, and a
    rule naming either holds only when the ask carries it."""

    text: str
    effect: str | None = None
    operation: str | None = None
    #: ``True``: only when the ask names its subject; ``False``: only when not.
    named: bool | None = None


@dataclass(frozen=True, slots=True)
class Presenter:
    key: str
    layer: Layer
    #: What selects it: permission kinds, capabilities or canonical kinds.
    matches: tuple[str, ...]
    #: The kind of action, as a short verb phrase: a card's header.
    label: str | None = None
    titles: tuple[TitleRule, ...] = ()
    #: The canonical kinds under which ``titles`` may ask the question; empty
    #: for every kind. A lane that words an action only the harness could not
    #: class (``other``) says so here.
    titles_when_canonical: tuple[str, ...] = ()
    format: SubjectFormat | None = None
    language: str | None = None
    #: Canonical only: what the card says while the subject is on its way.
    waiting: str | None = None
    #: Canonical only: the question already names the tool, so the card never
    #: adds a "Requested by" line under it.
    names_tool: bool = False
    #: Input keys a subject is recovered from when the ask arrived before the
    #: call naming it, tried before ``SUBJECT_INPUT_KEYS``.
    input_keys: tuple[str, ...] = ()
    #: Capability only: the noun a standing grant's rule reads with
    #: ("every git status command"); empty when the operation says it alone.
    scope_noun: str = "action"
    #: Capability only: whole scope phrases by operation, where the operation's
    #: words are not how a reader would say it.
    scope_phrases: dict[str, str] = field(default_factory=dict)
    scope_fallback: str | None = None


_REGISTRY: dict[tuple[Layer, str], Presenter] = {}
_ORDER: list[Presenter] = []


def register(presenter: Presenter) -> Presenter:
    """Add a presenter. A second entry for a key it already matches is a bug
    (two rows claiming one ask), so it raises rather than shadowing."""
    for match in presenter.matches:
        slot = (presenter.layer, match)
        if slot in _REGISTRY:
            raise ValueError(f"{presenter.layer} {match!r} is already presented")
        _REGISTRY[slot] = presenter
    _ORDER.append(presenter)
    return presenter


def lookup(layer: Layer, key: str) -> Presenter | None:
    return _REGISTRY.get((layer, key))


def presenters() -> tuple[Presenter, ...]:
    """Every registered presenter, in registration order."""
    return tuple(_ORDER)


# --------------------------------------------------------------------------
# Canonical: the base every ask has
# --------------------------------------------------------------------------

register(
    Presenter(
        key="shell",
        layer="canonical",
        matches=("shell",),
        label="Run a shell command",
        titles=(
            TitleRule("Run this command?", named=True),
            TitleRule("Run a command?"),
        ),
        format="command",
        language="bash",
        waiting="Waiting for the command…",
    )
)
register(
    Presenter(
        key="edit",
        layer="canonical",
        matches=("edit",),
        label="Edit files",
        titles=(
            TitleRule("Edit these files?", named=True),
            TitleRule("Edit a file?"),
        ),
        format="path",
        waiting="Waiting for the file…",
    )
)
register(
    Presenter(
        key="network",
        layer="canonical",
        matches=("network",),
        label="Fetch a web address",
        titles=(
            TitleRule("Fetch this address?", named=True),
            TitleRule("Fetch an address?"),
        ),
        format="url",
        waiting="Waiting for the address…",
    )
)
register(
    Presenter(
        key="task",
        layer="canonical",
        matches=("task",),
        label="Delegate to a subagent",
        titles=(TitleRule("Delegate this work?"),),
        format="code",
        waiting="Waiting for the work…",
    )
)
register(
    Presenter(
        key="external",
        layer="canonical",
        matches=("external",),
        label="Use an external tool",
        titles=(TitleRule("Allow {tool}?"), TitleRule("Allow this action?")),
        format="code",
        waiting="Waiting for the request…",
        names_tool=True,
    )
)
#: The fallback: a class nobody registered reads as ``other``.
GENERIC = register(
    Presenter(
        key="generic",
        layer="canonical",
        matches=("other",),
        label="Use a tool",
        titles=(TitleRule("Allow {tool}?"), TitleRule("Allow this action?")),
        format="code",
        waiting="Waiting for the request…",
        names_tool=True,
    )
)

# --------------------------------------------------------------------------
# Capability: the classifier's lanes
# --------------------------------------------------------------------------

# The harness's own lanes carry no words of their own -- the canonical class
# already asks the question -- but a standing grant still reads by their noun.
register(
    Presenter(
        key="shell_lane",
        layer="capability",
        matches=("shell",),
        format="code",
        scope_noun="command",
    )
)
register(
    Presenter(
        key="fs_lane", layer="capability", matches=("fs",), format="code", scope_noun="file action"
    )
)
register(
    Presenter(
        key="network_lane",
        layer="capability",
        matches=("network",),
        format="code",
        scope_noun="request",
    )
)
register(
    Presenter(
        key="sql",
        layer="capability",
        matches=("sql",),
        label="Run a SQL query",
        titles=(
            TitleRule("Run this destructive query?", effect="destroy"),
            TitleRule("Run this query? It exports data.", effect="egress"),
            TitleRule("Run this query?"),
        ),
        format="code",
        language="sql",
        scope_noun="statement",
        input_keys=("sql", "query", "statement"),
    )
)
register(
    Presenter(
        key="knowledge",
        layer="capability",
        matches=("knowledge",),
        label="Write to a knowledge base",
        titles=(
            TitleRule(f"Share with the {TEAM_KNOWLEDGE_BASE}?", operation="knowledge_share"),
            TitleRule(f"Withdraw from the {TEAM_KNOWLEDGE_BASE}?", operation="knowledge_unshare"),
            TitleRule(f"Save to the {PROJECT_KNOWLEDGE_BASE}?"),
        ),
        format="sentence",
        scope_phrases={
            "knowledge_share": f"share with the {TEAM_KNOWLEDGE_BASE}",
            "knowledge_unshare": f"withdrawal from the {TEAM_KNOWLEDGE_BASE}",
        },
        scope_fallback=f"save to the {PROJECT_KNOWLEDGE_BASE}",
    )
)
register(
    Presenter(
        key="mongodb",
        layer="capability",
        matches=("mongodb",),
        label="Run a MongoDB aggregation",
        titles=(
            TitleRule("Run this destructive aggregation?", effect="destroy"),
            TitleRule("Run this aggregation?"),
        ),
        format="code",
        scope_noun="aggregation",
    )
)
register(
    Presenter(
        key="elasticsearch",
        layer="capability",
        matches=("elasticsearch",),
        label="Send a search request",
        titles=(
            TitleRule("Send this destructive request?", effect="destroy"),
            TitleRule("Send this request?"),
        ),
        format="code",
        scope_noun="request",
    )
)
register(
    Presenter(
        key="integration_sdk",
        layer="capability",
        matches=("integration_sdk",),
        label="Run code against a connection",
        titles=(TitleRule("Run this code against {connection}?"), TitleRule("Run this code?")),
        format="code",
        language="python",
        scope_noun="SDK call",
    )
)
register(
    Presenter(
        key="graph_file",
        layer="capability",
        matches=("file",),
        label="Write a graph file",
        titles=(TitleRule("Write this graph file?"),),
        format="sentence",
        scope_noun="",
    )
)
#: A notebook tool's ask. The ask itself carries its words (``AskNotebook``:
#: "Run 3 cells in" a file); this entry is the question when it does not.
register(
    Presenter(
        key="notebook",
        layer="capability",
        matches=("notebook",),
        label="Use a notebook",
        titles=(TitleRule("Allow this notebook action?"),),
        format="sentence",
        scope_noun="notebook action",
    )
)
#: Any capability nobody registered: a control-plane action on a connection (a
#: warehouse kill, a job run, a cluster start), named by the app it belongs to.
#: Its question needs the connection; without one the class asks instead.
register(
    Presenter(
        key="connection_action",
        layer="capability",
        matches=("*",),
        label="Act on a connection",
        titles=(TitleRule("Allow this action on {connection}?"),),
        titles_when_canonical=("other",),
        format="sentence",
        scope_noun="action",
    )
)

# --------------------------------------------------------------------------
# Mechanism: gates on a capability or a guard, keyed by the ask itself
# --------------------------------------------------------------------------

for _key, _label, _title in (
    ("external_directory", "Reach outside the project", "Allow access outside the project?"),
    ("repo_clone", "Clone a repository", "Clone this repository?"),
    ("repo_overview", "Read a cached repository", "Read this cached repository?"),
    ("doom_loop", "Repeat a call", "Repeat this call again?"),
    ("workflow_tool_approval", "Run a workflow step", "Allow this workflow step?"),
):
    register(
        Presenter(
            key=_key, layer="mechanism", matches=(_key,), label=_label, titles=(TitleRule(_title),)
        )
    )


# --------------------------------------------------------------------------
# Tool names: how a wire key resolves to a tool a reader can name
# --------------------------------------------------------------------------

#: opencode-native tools: vendored TypeScript, not in the alkera tool manifest.
NATIVE_TOOL_NAMES: tuple[str, ...] = (
    "read",
    "write",
    "edit",
    "glob",
    "grep",
    "bash",
    "apply_patch",
    "lsp",
    "repo_clone",
    "repo_overview",
    "webfetch",
    "websearch",
    "todowrite",
    "skill",
)

#: Wire-name variants that mean the same native tool.
NATIVE_TOOL_ALIASES: dict[str, str] = {
    "ripgrep": "grep",
    "shell": "bash",
    "terminal": "bash",
    "patch": "apply_patch",
    "todo": "todowrite",
}


# --------------------------------------------------------------------------
# Redaction: what never reaches a permission prompt on any surface
# --------------------------------------------------------------------------

REDACTED = "[redacted]"


@dataclass(frozen=True, slots=True)
class RedactionRule:
    """A secret in free text. Group 1 is kept, group 2 is the secret, group 3
    (possibly empty) is kept after it. Written in the regex dialect JavaScript
    and Python share, so the TS twin runs the very same patterns."""

    name: str
    pattern: str
    ignore_case: bool = False


REDACTION_RULES: tuple[RedactionRule, ...] = (
    RedactionRule(
        "assignment",
        r"\b([A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY"
        r"|PRIVATE_?KEY|CREDENTIALS?)[A-Za-z0-9_]*[ \t]*[=:][ \t]*)"
        r'("[^"\n]*"|' + r"'[^'\n]*'|" + r'[^\s"' + r"',;&|]+)()",
        ignore_case=True,
    ),
    RedactionRule(
        "authorization",
        r"(authorization:[ \t]*(?:bearer|basic|token)[ \t]+)([A-Za-z0-9._~+/=-]+)()",
        ignore_case=True,
    ),
    RedactionRule(
        "flag",
        r"(--?(?:password|passwd|token|secret|api-key|apikey|access-key|client-secret)(?:=|[ \t]+))"
        r'("[^"\n]*"|' + r"'[^'\n]*'|" + r'[^\s"' + r"']+)()",
        ignore_case=True,
    ),
    RedactionRule(
        "url_userinfo",
        r"([a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)",
        ignore_case=True,
    ),
    RedactionRule(
        "token_shape",
        r"()((?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
        r"|alk_[A-Za-z0-9_]{16,}|AIza[0-9A-Za-z_-]{35}"
        r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})()",
    ),
)

#: A key in a call's input whose value is a secret whatever it looks like.
SENSITIVE_INPUT_KEY_PATTERN = (
    r"^(?:.*[_-])?(?:secret|token|password|passwd|api[_-]?key|apikey|private[_-]?key"
    r"|access[_-]?key|credentials?|authorization|cookie)$"
)


__all__ = [
    "DECISION_OUTCOMES",
    "EFFECT_REASONS",
    "EXACT_ALWAYS_LABEL",
    "EXEC_TITLE",
    "GENERIC",
    "NATIVE_TOOL_ALIASES",
    "NATIVE_TOOL_NAMES",
    "NOTEBOOK_PREVIEW_LINES",
    "NOTEBOOK_PREVIEW_WIDTH",
    "OPERATION_REASONS",
    "PROJECT_KNOWLEDGE_BASE",
    "REDACTED",
    "REDACTION_RULES",
    "SENSITIVE_INPUT_KEY_PATTERN",
    "SUBJECT_INPUT_KEYS",
    "TEAM_KNOWLEDGE_BASE",
    "UNSURE_REASON",
    "Layer",
    "Presenter",
    "RedactionRule",
    "TitleRule",
    "lookup",
    "presenters",
    "register",
]
