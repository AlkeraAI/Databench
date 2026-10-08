"""The two shapes of the permission presentation: what an ask carries, and what
a surface shows for it.

Both serialize in camelCase, because the same JSON is what the TypeScript twin
in ``@alkera/chat-model`` takes and produces: the conformance vectors are a
list of ``(PermissionAsk, PermissionPresentation)`` pairs, and each side must
turn the first into the second byte for byte.

Neither is persisted -- a presentation is derived on read from the
``permission.request`` event, which is the durable record -- so they are plain
models rather than versioned ones. ``PRESENTATION_VERSION`` rides every
presentation so a surface can tell which contract it is reading.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

#: Bumped when the presentation's shape or the rules producing it change in a
#: way a renderer must know about. Rendered into the generated TS twin, so the
#: two sides cannot disagree about which contract they implement.
PRESENTATION_VERSION = "1.1.0"


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


# --------------------------------------------------------------------------
# What an ask carries
# --------------------------------------------------------------------------


class AskTarget(_Camel):
    kind: str = "resource"
    name: str = ""
    connection: str | None = None


class AskCost(_Camel):
    usd: float | None = None
    bytes_scanned: int | None = None
    rows_scanned: int | None = None
    currency: str | None = None


class AskSubject(_Camel):
    """The classified action (``permission.request.subject``)."""

    capability: str | None = None
    effect: str | None = None
    #: How sure the classifier is of ``effect``: ``exact``, ``heuristic`` or
    #: ``unknown`` (it could not tell, and counted the action as a write).
    confidence: str | None = None
    operation: str | None = None
    targets: list[AskTarget] = Field(default_factory=list)
    cost: AskCost | None = None
    scope: str | None = None


#: Why a cell in a notebook run executes: the agent asked for it (``target``),
#: a target reads what it defines (``dependency``), or it reads what a target
#: defines (``dependent``).
NotebookCellRole = Literal["target", "dependency", "dependent"]


class AskNotebookCell(_Camel):
    """One cell a notebook ask would run, as the machine that holds the
    notebook names it for a person: never by its internal id."""

    name: str
    code: str = ""
    role: NotebookCellRole = "target"


class AskNotebook(_Camel):
    """A notebook tool's ask, in the words of the notebook it acts on.

    The action reads ``{lead} {file_name}{tail}``: "Run 3 cells in" +
    ``analysis.alknb.py``, "Restart the kernel of" + the file + " (ends work
    started by Bob)". ``file_path`` is the notebook's path in the workspace,
    for opening it; never a path on the machine's own disk."""

    lead: str
    file_name: str
    file_path: str
    tail: str = ""
    cells: list[AskNotebookCell] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)


class AskPreview(_Camel):
    kind: str = "diff"
    title: str | None = None
    content: str | None = None
    truncated: bool = False
    #: A notebook tool's ask (``kind == "notebook"``).
    notebook: AskNotebook | None = None


class AskOption(_Camel):
    option_id: str
    name: str


class AskCall(_Camel):
    """The tool call the ask gates, when the reader has seen it."""

    name: str = ""
    input: dict[str, Any] = Field(default_factory=dict)


class PermissionAsk(_Camel):
    """A pending permission ask, as both surfaces read it off the event."""

    permission_kind: str = "permission"
    canonical_kind: str = "other"
    patterns: list[str] = Field(default_factory=list)
    subject_pending: bool = False
    subject: AskSubject | None = None
    preview: AskPreview | None = None
    options: list[AskOption] = Field(default_factory=list)
    call: AskCall | None = None


# --------------------------------------------------------------------------
# What a surface shows
# --------------------------------------------------------------------------

#: How the subject reads: a shell line, a file path, a URL, code in a mono
#: inset (``language`` names its syntax), or a sentence in the reading face.
SubjectFormat = Literal["command", "path", "url", "code", "sentence"]

DecisionRole = Literal["allow", "always", "deny", "other"]

#: The fields a renderer must show whenever the presentation carries them. The
#: parity tests walk this list for every registered presenter and fail a
#: surface that drops one.
PrimaryField = Literal[
    "title", "subject", "note", "change", "details", "notebook", "facts", "decisions"
]


class PresentedSubject(_Camel):
    text: str
    format: SubjectFormat
    language: str | None = None


class PresentedNote(_Camel):
    """A knowledge item the ask would send: a heading and a body of prose."""

    title: str | None = None
    body: str
    truncated: bool = False


class PresentedChange(_Camel):
    """A proposed change to a file, as a unified diff."""

    title: str | None = None
    content: str


class PresentedDetails(_Camel):
    """The gated call itself, for an ask that names nothing else: the tool's
    name and its whole input, as JSON."""

    tool: str
    input: str


class PresentedNotebookCell(_Camel):
    """A cell as the card lists it: its name and the first lines of its code."""

    name: str
    preview_lines: list[str] = Field(default_factory=list)
    role: NotebookCellRole = "target"


class PresentedNotebook(_Camel):
    """A notebook ask: the action on the notebook, the cells it asked to run,
    the cells that run with them, and why it needs approval, once.

    ``title`` is ``{lead} {file_name}{tail}``; a surface that can open files
    renders ``file_name`` as a link to ``file_path``."""

    lead: str
    file_name: str
    file_path: str
    tail: str = ""
    #: Why the ask needs approval, in plain words.
    reason: str | None = None
    #: The cells the agent asked to run, in notebook order.
    cells: list[PresentedNotebookCell] = Field(default_factory=list)
    #: The cells that run only because of them, in notebook order.
    related: list[PresentedNotebookCell] = Field(default_factory=list)
    #: The one quiet line standing for ``related``: "Also runs 2 cells it depends on".
    related_line: str | None = None
    packages: list[str] = Field(default_factory=list)


class PresentedDecision(_Camel):
    option_id: str
    label: str
    role: DecisionRole


class PermissionPresentation(_Camel):
    version: str = PRESENTATION_VERSION
    #: The registry entry that described the ask (``generic`` for a fallback).
    presenter: str
    #: What kind of action it is, as a short verb phrase ("Run a shell command").
    label: str
    #: The question the reader answers.
    title: str
    subject: PresentedSubject | None = None
    #: Said in the subject's place when the ask named nothing and the tool that
    #: raised it is the last concrete fact it carries.
    missing_subject: str | None = None
    #: Said in the subject's place while the subject is still on its way.
    waiting: str | None = None
    note: PresentedNote | None = None
    change: PresentedChange | None = None
    details: PresentedDetails | None = None
    notebook: PresentedNotebook | None = None
    #: Targets and cost, each one short phrase.
    facts: list[str] = Field(default_factory=list)
    #: The classified effect tier (read / write / destroy / egress / exec / ...).
    effect: str | None = None
    #: What a standing grant would cover, when one is offered.
    always_scope: str | None = None
    decisions: list[PresentedDecision] = Field(default_factory=list)
    primary: list[PrimaryField] = Field(default_factory=list)


__all__ = [
    "PRESENTATION_VERSION",
    "AskCall",
    "AskCost",
    "AskNotebook",
    "AskNotebookCell",
    "AskOption",
    "AskPreview",
    "AskSubject",
    "AskTarget",
    "DecisionRole",
    "NotebookCellRole",
    "PermissionAsk",
    "PermissionPresentation",
    "PresentedChange",
    "PresentedDecision",
    "PresentedDetails",
    "PresentedNote",
    "PresentedNotebook",
    "PresentedNotebookCell",
    "PresentedSubject",
    "PrimaryField",
    "SubjectFormat",
]
