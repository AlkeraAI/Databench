"""Ask -> presentation: the one reading of a pending permission ask.

Pure: data in, data out. Every surface that shows a permission prompt renders
the :class:`PermissionPresentation` this returns (Slack here, the web through
the TypeScript twin in ``@alkera/chat-model``, which the conformance vectors
hold to this function's exact output). What the prompt SAYS is decided here and
in the registry, never in a renderer.
"""

from __future__ import annotations

import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from alkera_core.money import usd_display
from alkera_core.permission_presentation._alkera_tools import ALKERA_TOOL_NAMES
from alkera_core.permission_presentation.model import (
    AskCall,
    AskCost,
    AskNotebook,
    AskNotebookCell,
    AskOption,
    AskPreview,
    AskSubject,
    AskTarget,
    DecisionRole,
    NotebookCellRole,
    PermissionAsk,
    PermissionPresentation,
    PresentedChange,
    PresentedDecision,
    PresentedDetails,
    PresentedNote,
    PresentedNotebook,
    PresentedNotebookCell,
    PresentedSubject,
    PrimaryField,
)
from alkera_core.permission_presentation.registry import (
    EFFECT_REASONS,
    EXACT_ALWAYS_LABEL,
    EXEC_TITLE,
    GENERIC,
    NATIVE_TOOL_ALIASES,
    NATIVE_TOOL_NAMES,
    NOTEBOOK_PREVIEW_LINES,
    NOTEBOOK_PREVIEW_WIDTH,
    OPERATION_REASONS,
    REDACTED,
    REDACTION_RULES,
    SENSITIVE_INPUT_KEY_PATTERN,
    SUBJECT_INPUT_KEYS,
    UNSURE_REASON,
    Presenter,
    TitleRule,
    lookup,
)

# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

_RULES = tuple(
    re.compile(rule.pattern, re.IGNORECASE if rule.ignore_case else 0) for rule in REDACTION_RULES
)
_SECRET_KEY = re.compile(SENSITIVE_INPUT_KEY_PATTERN, re.IGNORECASE)


def redact(text: str) -> str:
    """``text`` with every secret the rules recognise replaced by ``[redacted]``."""
    for rule in _RULES:
        text = rule.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3) or ''}", text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if _SECRET_KEY.match(str(key)) else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


# --------------------------------------------------------------------------
# JavaScript-compatible formatting (the TS twin must produce identical text)
# --------------------------------------------------------------------------


def js_number(value: float) -> str:
    """``String(value)`` as JavaScript spells it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if math.isnan(value) or math.isinf(value):
        return "null"
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    sign, digits_t, exponent = Decimal(repr(value)).as_tuple()
    assert isinstance(exponent, int)
    digits = "".join(str(d) for d in digits_t).rstrip("0") or "0"
    exponent += len(digits_t) - len(digits)
    k = len(digits)
    n = exponent + k
    prefix = "-" if sign else ""
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = f"{digits[:n]}.{digits[n:]}"
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mantissa = digits[0] + (f".{digits[1:]}" if k > 1 else "")
        body = f"{mantissa}e{'+' if e > 0 else '-'}{abs(e)}"
    return prefix + body


def js_to_fixed(value: float, places: int) -> str:
    """``value.toFixed(places)``: the exact binary value, ties away from zero."""
    quantum = Decimal(1).scaleb(-places)
    return str(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))


def js_json(value: Any, indent: str = "") -> str:
    """``JSON.stringify(value, null, 2)``."""
    step = indent + "  "
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return js_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if not value:
            return "[]"
        items = ",\n".join(step + js_json(item, step) for item in value)
        return f"[\n{items}\n{indent}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = ",\n".join(
            f"{step}{json.dumps(str(key), ensure_ascii=False)}: {js_json(item, step)}"
            for key, item in value.items()
        )
        return f"{{\n{items}\n{indent}}}"
    return json.dumps(str(value), ensure_ascii=False)


_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(size: float) -> str:
    value = float(size)
    unit = 0
    while value >= 1024 and unit < len(_BYTE_UNITS) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{math.floor(value + 0.5)} {_BYTE_UNITS[unit]}"
    return f"{js_to_fixed(value, 1 if value < 10 else 0)} {_BYTE_UNITS[unit]}"


# --------------------------------------------------------------------------
# Tool names
# --------------------------------------------------------------------------

_ALKERA_BY_SPELLING: dict[str, str] = {}
for _name in ALKERA_TOOL_NAMES:
    _ALKERA_BY_SPELLING[_name.lower()] = _name
    _ALKERA_BY_SPELLING[re.sub(r"[^a-zA-Z0-9_-]", "_", _name).lower()] = _name
_NATIVE = frozenset(NATIVE_TOOL_NAMES)


def canonical_tool_name(name: str) -> str:
    """The wire name with its MCP server and ``alkera`` prefixes stripped."""
    stripped = re.sub(r"^mcp__[^_]+__", "", name.strip(), flags=re.IGNORECASE)
    return re.sub(r"^alkera[_-]", "", stripped, flags=re.IGNORECASE)


def resolve_tool(wire: str) -> str | None:
    """The tool a wire spelling denotes, or ``None`` when it names no tool."""
    key = canonical_tool_name(wire).lower()
    alkera = _ALKERA_BY_SPELLING.get(key)
    if alkera is not None:
        return alkera
    native = NATIVE_TOOL_ALIASES.get(key, key)
    return native if native in _NATIVE else None


# --------------------------------------------------------------------------
# The presentation
# --------------------------------------------------------------------------


def _capability(ask: PermissionAsk) -> str | None:
    if ask.subject and ask.subject.capability:
        return ask.subject.capability
    return "sql" if ask.permission_kind == "sql" else None


def _connection(ask: PermissionAsk) -> str | None:
    for target in ask.subject.targets if ask.subject else []:
        if target.connection:
            return target.connection
    return None


def _title(
    presenter: Presenter | None,
    *,
    canonical: str,
    effect: str | None,
    operation: str,
    named: bool,
    tool: str | None,
    connection: str | None,
) -> str | None:
    if presenter is None:
        return None
    if presenter.titles_when_canonical and canonical not in presenter.titles_when_canonical:
        return None
    for rule in presenter.titles:
        if _holds(rule, effect=effect, operation=operation, named=named):
            if "{tool}" in rule.text and not tool:
                continue
            if "{connection}" in rule.text and not connection:
                continue
            return rule.text.replace("{tool}", tool or "").replace("{connection}", connection or "")
    return None


def _holds(rule: TitleRule, *, effect: str | None, operation: str, named: bool) -> bool:
    if rule.effect is not None and rule.effect != effect:
        return False
    if rule.operation is not None and rule.operation != operation:
        return False
    return rule.named is None or rule.named == named


def _recovered_subject(ask: PermissionAsk, layers: list[Presenter]) -> str | None:
    if ask.call is None:
        return None
    keys: list[str] = []
    for presenter in layers:
        keys.extend(presenter.input_keys)
    keys.extend(SUBJECT_INPUT_KEYS)
    for key in keys:
        value = ask.call.input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _cost_facts(cost: AskCost | None) -> list[str]:
    if cost is None:
        return []
    facts: list[str] = []
    if cost.usd:
        if cost.currency in (None, "", "usd"):
            facts.append(usd_display(cost.usd))
        else:
            facts.append(f"{js_number(cost.usd)} {cost.currency}")
    if cost.bytes_scanned:
        facts.append(f"{format_bytes(cost.bytes_scanned)} scanned")
    elif cost.rows_scanned:
        facts.append(f"{cost.rows_scanned:,} rows scanned")
    return facts


#: The target kinds a notebook ask's classifier names: the notebook file itself.
_NOTEBOOK_TARGETS = frozenset({"notebook", "file"})


def _facts(subject: AskSubject | None, *, skip: frozenset[str] = frozenset()) -> list[str]:
    if subject is None:
        return []
    targets = ", ".join(
        target.name for target in subject.targets if target.name and target.kind not in skip
    )
    return [fact for fact in (targets, *_cost_facts(subject.cost)) if fact]


def always_scope(label: str, subject: AskSubject | None) -> str | None:
    """The one line saying what a standing grant under ``label`` would cover,
    or ``None`` when the classifier named nothing to say it about."""
    if (subject is not None and subject.scope == "command") or label == EXACT_ALWAYS_LABEL:
        said = "Always allow" if label == EXACT_ALWAYS_LABEL else label
        return f"{said} covers this exact command."
    capability = subject.capability if subject else None
    operation = subject.operation if subject else None
    if not capability or not operation or operation == "unknown":
        return None
    lane = lookup("capability", capability) or lookup("capability", "*")
    assert lane is not None
    if lane.scope_phrases or lane.scope_fallback:
        phrase = lane.scope_phrases.get(operation, lane.scope_fallback or operation)
        return f"{label} covers every {phrase}."
    words = operation.replace("_", " ")
    covered = f"{words} {lane.scope_noun}" if lane.scope_noun else words
    return f"{label} covers every {covered}."


def _always_line(
    always: AskOption | None, subject: AskSubject | None, notebook_raw: str | None
) -> str | None:
    if always is None:
        return None
    if notebook_raw and subject is not None and subject.scope == "command":
        return notebook_always_scope(always.name, notebook_raw)
    return always_scope(always.name, subject)


def requested_by(ask: PermissionAsk) -> str | None:
    """The line naming the tool that raised an ask, where the title does not."""
    base = lookup("canonical", ask.canonical_kind) or GENERIC
    if base.names_tool or lookup("mechanism", ask.permission_kind):
        return None
    tool = resolve_tool(ask.permission_kind)
    return f"Requested by the {tool} tool." if tool else None


# --------------------------------------------------------------------------
# Notebook asks
# --------------------------------------------------------------------------

#: The whitespace a code line is trimmed of: spelled out so the TS twin trims
#: exactly the same characters.
_TRIM = " \t\r\f\v"


def effect_reason(subject: AskSubject | None) -> str | None:
    """Why an action needs approval, in plain words: what the operation does
    where the effect words would misdescribe it, else that the classifier could
    not tell, else the effect it found."""
    if subject is None:
        return None
    by_operation = OPERATION_REASONS.get(subject.operation or "")
    if by_operation is not None:
        return by_operation
    if subject.confidence == "unknown":
        return UNSURE_REASON
    return EFFECT_REASONS.get(subject.effect or "")


def _clip(line: str, width: int) -> str:
    return line if len(line) <= width else line[: width - 1] + "…"


def code_preview(code: str) -> list[str]:
    """The first lines of a cell's code, enough to recognise it: blank lines
    skipped, each line cut to the preview width, and an ellipsis on the last
    line shown when more follow."""
    lines = [line.rstrip(_TRIM) for line in redact(code).split("\n") if line.strip(_TRIM)]
    shown = [_clip(line, NOTEBOOK_PREVIEW_WIDTH) for line in lines[:NOTEBOOK_PREVIEW_LINES]]
    if len(lines) > NOTEBOOK_PREVIEW_LINES and shown and not shown[-1].endswith("…"):
        shown[-1] = f"{shown[-1]} …"
    return shown


def _cell(cell: AskNotebookCell) -> PresentedNotebookCell:
    return PresentedNotebookCell(
        name=redact(cell.name), preview_lines=code_preview(cell.code), role=cell.role
    )


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def related_line(targets: int, dependencies: int, dependents: int) -> str | None:
    """The quiet line standing for the cells that run only because of the
    targets: "Also runs 2 cells it depends on"."""
    single = targets == 1
    parts: list[str] = []
    if dependencies:
        tail = "it depends on" if single else "they depend on"
        parts.append(f"{_count(dependencies, 'cell', 'cells')} {tail}")
    if dependents:
        verb = "depends" if dependents == 1 else "depend"
        parts.append(
            f"{_count(dependents, 'cell', 'cells')} that {verb} on {'it' if single else 'them'}"
        )
    return f"Also runs {' and '.join(parts)}" if parts else None


def _notebook(ask: AskNotebook, subject: AskSubject | None) -> PresentedNotebook:
    targets = [_cell(c) for c in ask.cells if c.role == "target"]
    related = [_cell(c) for c in ask.cells if c.role != "target"]
    if not targets:
        # Nothing was asked for by name (a widget change runs what reads it):
        # every cell is the run itself.
        targets, related = related, []
    dependencies = sum(1 for c in related if c.role == "dependency")
    return PresentedNotebook(
        lead=redact(ask.lead),
        file_name=ask.file_name,
        file_path=ask.file_path,
        tail=redact(ask.tail),
        reason=effect_reason(subject),
        cells=targets,
        related=related,
        related_line=related_line(len(targets), dependencies, len(related) - dependencies),
        packages=[redact(p) for p in ask.packages],
    )


def notebook_always_scope(label: str, raw: str) -> str:
    """What a standing grant on a notebook ask remembers: the action as the
    machine words it, whatever the cells hold."""
    return f"{label} skips this question whenever the agent asks to {raw[:1].lower()}{raw[1:]}."


_ROLES: dict[str, DecisionRole] = {
    "allow_once": "allow",
    "allow_always": "always",
    "reject_once": "deny",
    "reject_always": "deny",
    "cancelled": "deny",
}


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def present(ask: PermissionAsk) -> PermissionPresentation:
    """The presentation of one pending ask."""
    base = lookup("canonical", ask.canonical_kind) or GENERIC
    mechanism = lookup("mechanism", ask.permission_kind)
    capability = _capability(ask)
    lane = (lookup("capability", capability) or lookup("capability", "*")) if capability else None
    subject = ask.subject
    effect = subject.effect if subject else None
    operation = (subject.operation if subject else None) or ""
    connection = _connection(ask)
    tool = None if mechanism else resolve_tool(ask.permission_kind)

    layers = [p for p in (lane, base) if p is not None]
    raw = ask.patterns[0] if ask.patterns and ask.patterns[0] else _recovered_subject(ask, layers)
    named = bool(raw)
    title_args: dict[str, Any] = {
        "canonical": ask.canonical_kind,
        "effect": effect,
        "operation": operation,
        "named": named,
        "tool": tool,
        "connection": connection,
    }

    chosen: Presenter
    if mechanism is not None:
        chosen, title = mechanism, mechanism.titles[0].text
    elif effect == "exec":
        chosen, title = lane or base, EXEC_TITLE
    else:
        lane_title = _title(lane, **title_args)
        if lane is not None and lane_title is not None:
            chosen, title = lane, lane_title
        else:
            chosen = base
            title = _title(base, **title_args) or "Allow this action?"

    fmt = lane.format if (lane and lane.format and base is GENERIC) else base.format
    fmt = fmt or "code"
    language = (lane.language if lane else None) or base.language

    note: PresentedNote | None = None
    change: PresentedChange | None = None
    notebook: PresentedNotebook | None = None
    preview = ask.preview
    if preview is not None and preview.notebook is not None:
        notebook = _notebook(preview.notebook, subject)
        nb = notebook
        title = f"{nb.lead} {nb.file_name}{nb.tail}"
    elif preview is not None and preview.kind == "text" and preview.content is not None:
        note = PresentedNote(
            title=redact(preview.title) if preview.title else None,
            body=redact(preview.content),
            truncated=preview.truncated,
        )
    elif preview is not None and preview.content:
        change = PresentedChange(title=preview.title, content=redact(preview.content))

    presented_subject: PresentedSubject | None = None
    if raw and note is None and notebook is None:
        text = redact(raw)
        presented_subject = PresentedSubject(
            text=_sentence(text) if fmt == "sentence" else text, format=fmt, language=language
        )

    waiting = base.waiting if ask.subject_pending and not raw else None
    missing = (
        requested_by(ask)
        if presented_subject is None and note is None and notebook is None and waiting is None
        else None
    )

    details: PresentedDetails | None = None
    if (
        ask.call is not None
        and ask.call.input
        and presented_subject is None
        and note is None
        and change is None
        and notebook is None
        and waiting is None
    ):
        shown = resolve_tool(ask.call.name) or canonical_tool_name(ask.call.name) or tool or "tool"
        details = PresentedDetails(tool=shown, input=redact(js_json(_redact_value(ask.call.input))))

    always = next((o for o in ask.options if o.option_id == "allow_always"), None)
    decisions = [
        PresentedDecision(
            option_id=option.option_id,
            label=option.name,
            role=_ROLES.get(option.option_id, "other"),
        )
        for option in ask.options
    ]
    # A notebook ask names its notebook itself; the targets the classifier
    # recorded for it are paths on the machine, which mean nothing to a reader.
    facts = _facts(subject, skip=_NOTEBOOK_TARGETS if notebook is not None else frozenset())
    fields: tuple[tuple[PrimaryField, object], ...] = (
        ("title", title),
        ("subject", presented_subject),
        ("note", note),
        ("change", change),
        ("details", details),
        ("notebook", notebook),
        ("facts", facts),
        ("decisions", decisions),
    )
    primary: list[PrimaryField] = [name for name, value in fields if value]

    return PermissionPresentation(
        presenter=chosen.key,
        label=chosen.label or base.label or GENERIC.label or "",
        title=title,
        subject=presented_subject,
        missing_subject=missing,
        waiting=waiting,
        note=note,
        change=change,
        details=details,
        notebook=notebook,
        facts=facts,
        effect=effect,
        always_scope=_always_line(always, subject, raw if notebook is not None else None),
        decisions=decisions,
        primary=primary,
    )


# --------------------------------------------------------------------------
# Reading the ask off the event
# --------------------------------------------------------------------------

_CANONICAL = frozenset({"edit", "shell", "network", "task", "external"})
_EFFECTS = frozenset({"read", "write", "destroy", "egress", "exec", "memory"})
_PREVIEW_KINDS = frozenset({"diff", "table", "json", "terminal", "notebook"})
_CONFIDENCES = frozenset({"exact", "heuristic", "unknown"})


def _str(source: Any, key: str) -> str | None:
    value = source.get(key) if isinstance(source, dict) else None
    return value if isinstance(value, str) else None


def _num(source: Any, key: str) -> float | None:
    value = source.get(key) if isinstance(source, dict) else None
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _list(source: Any, key: str) -> list[Any]:
    value = source.get(key) if isinstance(source, dict) else None
    return value if isinstance(value, list) else []


def _subject(raw: Any) -> AskSubject | None:
    if not isinstance(raw, dict):
        return None
    targets: list[AskTarget] = []
    for target in _list(raw, "targets"):
        name, kind = _str(target, "name"), _str(target, "kind")
        if not name and not kind:
            continue
        targets.append(
            AskTarget(
                kind=kind or "resource", name=name or "", connection=_str(target, "connection")
            )
        )
    cost_raw = raw.get("cost_estimate")
    cost: AskCost | None = None
    if isinstance(cost_raw, dict):
        usd, scanned, rows = (
            _num(cost_raw, "usd_amount"),
            _num(cost_raw, "bytes_scanned"),
            _num(cost_raw, "rows_scanned"),
        )
        if usd is not None or scanned is not None or rows is not None:
            cost = AskCost(
                usd=usd,
                bytes_scanned=int(scanned) if scanned is not None else None,
                rows_scanned=int(rows) if rows is not None else None,
                currency=_str(cost_raw, "wallet_currency"),
            )
    confidence = _str(raw, "confidence") or None
    effect_raw = _str(raw, "effect")
    effect = None if not effect_raw else (effect_raw if effect_raw in _EFFECTS else "unknown")
    capability = _str(raw, "capability") or None
    operation = _str(raw, "operation") or None
    reasons = [r for r in _list(raw, "reasons") if isinstance(r, str)]
    if not (capability or effect or operation or targets or cost or reasons):
        return None
    return AskSubject(
        capability=capability,
        effect=effect,
        confidence=confidence if confidence in _CONFIDENCES else None,
        operation=operation,
        targets=targets,
        cost=cost,
        scope="command" if _str(raw, "scope") == "command" else None,
    )


def _cell_role(cell: Any) -> NotebookCellRole:
    """A cell's role as the event names it; one this reader does not know is a
    target, so the cell is shown rather than folded away."""
    role = _str(cell, "role")
    if role == "dependency":
        return "dependency"
    if role == "dependent":
        return "dependent"
    return "target"


def _notebook_ask(raw: Any) -> AskNotebook | None:
    if not isinstance(raw, dict):
        return None
    lead, file_name = _str(raw, "lead"), _str(raw, "file_name")
    if not lead or not file_name:
        return None
    cells: list[AskNotebookCell] = []
    for cell in _list(raw, "cells"):
        name = _str(cell, "name")
        if not name:
            continue
        cells.append(
            AskNotebookCell(name=name, code=_str(cell, "code") or "", role=_cell_role(cell))
        )
    return AskNotebook(
        lead=lead,
        file_name=file_name,
        file_path=_str(raw, "file_path") or file_name,
        tail=_str(raw, "tail") or "",
        cells=cells,
        packages=[p for p in _list(raw, "packages") if isinstance(p, str)],
    )


def _preview(raw: Any) -> AskPreview | None:
    if not isinstance(raw, dict):
        return None
    kind = _str(raw, "kind")
    notebook = _notebook_ask(raw.get("notebook")) if kind == "notebook" else None
    return AskPreview(
        kind=kind if kind in _PREVIEW_KINDS and (kind != "notebook" or notebook) else "text",
        title=_str(raw, "title"),
        content=_str(raw, "content"),
        truncated=raw.get("truncated") is True,
        notebook=notebook,
    )


def ask_from_event(payload: dict[str, Any], call: AskCall | None = None) -> PermissionAsk:
    """The ask a ``permission.request`` event carries, read the way the web's
    transcript fold reads it, plus the gated call when the reader has it."""
    canonical = _str(payload, "canonical_kind")
    return PermissionAsk(
        permission_kind=_str(payload, "permission_kind") or "permission",
        canonical_kind=canonical if canonical in _CANONICAL else "other",
        patterns=[p for p in _list(payload, "patterns") if isinstance(p, str)],
        subject_pending=payload.get("subject_pending") is True,
        subject=_subject(payload.get("subject")),
        preview=_preview(payload.get("preview")),
        options=[
            AskOption(
                option_id=_str(option, "option_id") or "reject_once",
                name=_str(option, "name") or "Reject",
            )
            for option in _list(payload, "options")
        ],
        call=call,
    )


__all__ = [
    "always_scope",
    "ask_from_event",
    "canonical_tool_name",
    "code_preview",
    "effect_reason",
    "format_bytes",
    "js_json",
    "js_number",
    "js_to_fixed",
    "notebook_always_scope",
    "present",
    "redact",
    "related_line",
    "requested_by",
    "resolve_tool",
]
