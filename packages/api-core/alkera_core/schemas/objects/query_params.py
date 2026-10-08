"""Compiling a saved query's slots to bound parameters.

A saved ``query`` object keeps a SQL template with slots and typed parameter
declarations; a re-run supplies the values. The template is never rendered with
those values. :func:`compile_query` rewrites every slot to the engine's own
placeholder — psycopg's ``%(customer)s`` on Postgres and Redshift, ClickHouse's
``{customer:String}`` on ClickHouse, Tinybird's ``{{String(customer)}}`` on
Tinybird, ``$customer`` on DuckDB, ``:customer`` on SQLite — and hands the
values back in a dict the driver binds on its side of the wire. The SQL string
that reaches the driver therefore holds placeholders and never a value, so a
value cannot close a quote, open a comment or widen a ``WHERE``; and the
permission classifier judges the same parameterized text the driver executes.
An engine with no bound-parameter path here is refused, never interpolated.

**A slot has two spellings, and both are this grammar.** ``{customer}`` is the
compiler's own, and ``{{String(customer)}}`` is Tinybird's — one of
``String`` / ``Int32`` / ``Int64`` / ``Float64`` / ``Date`` / ``DateTime`` /
``Boolean`` naming the type the engine re-validates the value against. Both are
read here, because a real agent asked for a parameterised statement writes the
engine's syntax, not ours: the browser rehearsal saved a deliverable whose
``WHERE`` carried ``{{String(customer_id)}}`` and two ``{{Date(…)}}`` bounds,
and a reader that knew only ``{name}`` told the analyst the query took no
parameters. A typed slot carries its own declaration, so it compiles to the
type it was written with — a ``{{DateTime(t)}}`` stays a ``DateTime`` on
Tinybird and a ``{t:DateTime}`` on ClickHouse rather than being flattened to a
date — and on Tinybird the compiled statement is byte-identical to the template
it came from, which is exactly what lets it pass the driver's directive check.

**Tinybird is not ClickHouse here, and the difference is load-bearing.**
ClickHouse's own HTTP interface binds ``{customer:String}`` from a ``param_``
field, but Tinybird's ``/v0/sql`` puts its own template engine in front of the
database: it reads the braces first, decides ``customer`` names a workspace
SECRET, and answers ``400 Cannot access secret 'customer'`` — the value never
binds. Tinybird's own parameter syntax is ``{{String(customer)}}`` in a
statement the driver marks as a template, with the value beside the statement
as a form field, and it is what this compiler emits for the ``tinybird``
engine. The values still never enter the SQL text: Tinybird quotes and escapes
each one on its side, and a value that is itself ``{{String(other)}}`` comes
back as those literal characters rather than being read again.

Values are typed. A declaration (the query object's ``params``) pins each
slot's type — ``string``, ``integer``, ``number``, ``boolean``, ``date``,
``datetime``, ``daterange``, ``enum`` — and a value of the wrong shape is refused before
anything is compiled. An undeclared slot takes the JSON type of its value: a
string stays a string (a date-shaped one included), an integer an integer, a
finite number a number, a boolean a boolean, and a non-empty list of one
scalar type expands to one placeholder per item inside parentheses (so
``IN {ids}`` works). An object is refused unless a declaration makes it a
``daterange``, which a template reads as ``{name.start}`` and ``{name.end}``.

Result rows are data, never instructions; so are parameter values — a value is
bound, never parsed for meaning, and the template author owns the SQL.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

ParamType = Literal[
    "string", "integer", "number", "boolean", "date", "datetime", "daterange", "enum"
]
PARAM_TYPES: frozenset[str] = frozenset(
    ("string", "integer", "number", "boolean", "date", "datetime", "daterange", "enum")
)

#: Tinybird's template type functions and the value kind each one names. These
#: are the types Tinybird's own engine parses a form field back to and REFUSES
#: when it does not fit (``Error validating '1 OR 1=1' to type Int64``), so a
#: slot written this way carries its declaration in the statement itself.
TEMPLATE_SLOT_TYPES: dict[str, ParamType] = {
    "String": "string",
    "Int32": "integer",
    "Int64": "integer",
    "Float64": "number",
    "Date": "date",
    "DateTime": "datetime",
    "Boolean": "boolean",
}

#: Longest first, so ``DateTime`` is not read as ``Date`` followed by junk.
_TYPE_NAMES = "|".join(sorted(TEMPLATE_SLOT_TYPES, key=len, reverse=True))

#: The one slot grammar, in three alternatives and in this order:
#:
#: 1. a typed slot — ``{{String(name)}}``, whitespace tolerated;
#: 2. ANY OTHER doubled-brace group, matched only so it is CONSUMED and
#:    discarded. Without it a bare ``{{customer}}`` — Tinybird's interpolation
#:    directive, which writes text into the statement rather than binding
#:    beside it — would be read as our own ``{customer}`` slot one character
#:    in, and compiled into ``{%(customer)s}``;
#: 3. a bare slot — ``{name}``, ``{name.start}`` / ``{name.end}``.
#:
#: Everything else in braces is the author's SQL and is left alone: a JSON
#: literal, a native ClickHouse ``{x:Type}`` placeholder, an unknown type
#: function, a spaced or numeric name. The browser's ``slotsOf``
#: (``apps/web/src/lib/querySlots.ts``) is the same three alternatives in the
#: same order, and one fixture drives both.
SLOT = re.compile(
    r"\{\{\s*(?P<type>" + _TYPE_NAMES + r")\s*\(\s*(?P<typed>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*\}\}"
    r"|\{\{[^{}]*\}\}"
    r"|\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\.(?P<part>start|end))?\}"
)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INTEGER = re.compile(r"^-?\d{1,19}$")

PlaceholderStyle = Literal["pyformat", "clickhouse", "tinybird", "dollar", "colon"]

#: Engines with a bound-parameter path the SQL connector can drive, by the
#: placeholder grammar their driver binds on its own side. The permission
#: classifier parses every one of these grammars in the engine's dialect, so a
#: compiled statement still classifies exactly. ``tinybird`` is deliberately
#: NOT ``clickhouse``: the same braces mean a workspace secret there, and only
#: Tinybird's own ``{{Type(name)}}`` template slot binds a value (see above).
ENGINE_STYLES: dict[str, PlaceholderStyle] = {
    "postgres": "pyformat",
    "redshift": "pyformat",
    "clickhouse": "clickhouse",
    "tinybird": "tinybird",
    "duckdb": "dollar",
    "duckdb_local": "dollar",
    "sqlite": "colon",
}

MAX_STRING_CHARS = 8192
MAX_LIST_ITEMS = 1000
MAX_PARAMS = 100
MAX_ENUM_VALUES = 500
_INT64_MAX = 2**63 - 1
_INT64_MIN = -(2**63)


class QueryCompileError(ValueError):
    """The template, the declarations and the values do not fit each other, or
    the engine has no bound-parameter path."""


@dataclass(frozen=True, slots=True)
class QueryParamDeclaration:
    """One typed slot of a saved query."""

    name: str
    type: ParamType = "string"
    enum_values: tuple[str, ...] = ()
    required: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> QueryParamDeclaration:
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME.match(name):
            raise QueryCompileError(f"a parameter declaration names an invalid slot {name!r}")
        kind = raw.get("type", "string")
        if not isinstance(kind, str) or kind not in PARAM_TYPES:
            raise QueryCompileError(f"parameter {name} has an unknown type {kind!r}")
        values_raw = raw.get("enum_values")
        values: tuple[str, ...] = ()
        if values_raw is not None:
            if not isinstance(values_raw, list | tuple) or not all(
                isinstance(v, str) for v in values_raw
            ):
                raise QueryCompileError(f"parameter {name}: enum_values must be a list of strings")
            values = tuple(values_raw)
        required = raw.get("required", True)
        if not isinstance(required, bool):
            raise QueryCompileError(f"parameter {name}: required must be a boolean")
        return cls(
            name=name,
            type=kind,  # type: ignore[arg-type]  # membership in PARAM_TYPES checked above
            enum_values=values,
            required=required,
        )


@dataclass(frozen=True, slots=True)
class BoundQuery:
    """The statement to execute and the values to bind beside it."""

    sql: str
    params: dict[str, Any]
    engine: str
    placeholders: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuerySlot:
    """One slot as the template spells it: its name, the type that spelling
    implies, and — for Tinybird's form — the type function it was written with,
    which is the text compiled back out on that engine."""

    name: str
    type: ParamType
    template_type: str = ""


def _slot_name(match: re.Match[str]) -> str | None:
    """The slot's name, or ``None`` when the match is a doubled-brace group
    that is NOT a slot — consumed by the grammar so it cannot be read as one,
    and left in the statement as the author's own text."""
    return match.group("typed") or match.group("name")


def slots_of(template: str) -> tuple[QuerySlot, ...]:
    """Every distinct slot in first-appearance order, with the type its
    spelling declares.

    A bare ``{name}`` declares nothing and defaults to ``string`` (the value's
    own JSON type is inferred at compile time when no declaration overrides
    it); ``{name.start}`` / ``{name.end}`` make one ``daterange``, and a part
    outranks a bare use of the same name; a typed ``{{Int64(name)}}`` declares
    its type, and the FIRST typed spelling of a name is the one that counts.

    The browser's ``slotsOf`` answers the same for the same template — one
    fixture drives both — because a slot one side sees and the other does not
    is a re-run form that cannot bind or a form field that never appears.
    """
    found: dict[str, QuerySlot] = {}
    for match in SLOT.finditer(template):
        name = _slot_name(match)
        if name is None:
            continue
        type_text = match.group("type") or ""
        kind: ParamType = (
            TEMPLATE_SLOT_TYPES[type_text]
            if type_text
            else ("daterange" if match.group("part") else "string")
        )
        current = found.get(name)
        if current is None:
            found[name] = QuerySlot(name=name, type=kind, template_type=type_text)
            continue
        if kind == "daterange":
            found[name] = QuerySlot(name=name, type=kind, template_type=current.template_type)
        elif type_text and not current.template_type and current.type != "daterange":
            found[name] = QuerySlot(name=name, type=kind, template_type=type_text)
    return tuple(found.values())


def placeholders_of(template: str) -> tuple[str, ...]:
    """The distinct slot names in first-appearance order."""
    return tuple(slot.name for slot in slots_of(template))


# --------------------------------------------------------------------------- #
# value typing
# --------------------------------------------------------------------------- #

Scalar = str | int | float | bool | date | None


@dataclass(frozen=True, slots=True)
class _Scalar:
    value: Scalar


@dataclass(frozen=True, slots=True)
class _List:
    values: tuple[Scalar, ...]


@dataclass(frozen=True, slots=True)
class _DateRange:
    start: date
    end: date


_Bound = _Scalar | _List | _DateRange


def _string(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise QueryCompileError(f"parameter {name} must be a string")
    if "\x00" in value:
        raise QueryCompileError(f"parameter {name} may not contain a NUL byte")
    if len(value) > MAX_STRING_CHARS:
        raise QueryCompileError(f"parameter {name} is longer than {MAX_STRING_CHARS} characters")
    return value


def _integer(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise QueryCompileError(f"parameter {name} must be an integer, not a boolean")
    if isinstance(value, int):
        out = value
    elif isinstance(value, str) and _INTEGER.match(value.strip()):
        out = int(value.strip())
    else:
        raise QueryCompileError(f"parameter {name} must be an integer")
    if not _INT64_MIN <= out <= _INT64_MAX:
        raise QueryCompileError(f"parameter {name} is outside the 64-bit integer range")
    return out


def _number(name: str, value: Any) -> int | float:
    if isinstance(value, bool):
        raise QueryCompileError(f"parameter {name} must be a number, not a boolean")
    if isinstance(value, int):
        return _integer(name, value)
    out: float
    if isinstance(value, float | Decimal):
        out = float(value)
    elif isinstance(value, str):
        try:
            out = float(Decimal(value.strip()))
        except (InvalidOperation, ValueError):
            raise QueryCompileError(f"parameter {name} must be a number") from None
    else:
        raise QueryCompileError(f"parameter {name} must be a number")
    if not math.isfinite(out):
        raise QueryCompileError(f"parameter {name} must be a finite number")
    return out


def _boolean(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise QueryCompileError(f"parameter {name} must be true or false")


def _date(name: str, value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            raise QueryCompileError(f"parameter {name} is not a calendar date") from None
    raise QueryCompileError(f"parameter {name} must be a date (YYYY-MM-DD)")


def _datetime(name: str, value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            raise QueryCompileError(
                f"parameter {name} must be a date and time (YYYY-MM-DD HH:MM:SS)"
            ) from None
    raise QueryCompileError(f"parameter {name} must be a date and time (YYYY-MM-DD HH:MM:SS)")


def _daterange(name: str, value: Any) -> _DateRange:
    if isinstance(value, Mapping):
        if set(value) != {"start", "end"}:
            raise QueryCompileError(f"parameter {name} must be {{start, end}}")
        start_raw, end_raw = value["start"], value["end"]
    elif isinstance(value, list | tuple) and len(value) == 2:
        start_raw, end_raw = value
    else:
        raise QueryCompileError(f"parameter {name} must be a date range {{start, end}}")
    start = _date(f"{name}.start", start_raw)
    end = _date(f"{name}.end", end_raw)
    if start > end:
        raise QueryCompileError(f"parameter {name}: start is after end")
    return _DateRange(start=start, end=end)


def _enum(name: str, value: Any, allowed: tuple[str, ...]) -> str:
    if not allowed:
        raise QueryCompileError(f"parameter {name} is an enum with no values")
    if len(allowed) > MAX_ENUM_VALUES:
        raise QueryCompileError(f"parameter {name} lists more than {MAX_ENUM_VALUES} values")
    if not isinstance(value, str) or value not in allowed:
        raise QueryCompileError(f"parameter {name} must be one of the declared values")
    return value


def _scalar_of_kind(name: str, value: Any, kind: ParamType) -> Scalar:
    """One value as the named scalar type. ``daterange`` and ``enum`` are not
    scalars and never reach here."""
    if kind == "string":
        return _string(name, value)
    if kind == "integer":
        return _integer(name, value)
    if kind == "number":
        return _number(name, value)
    if kind == "boolean":
        return _boolean(name, value)
    if kind == "date":
        return _date(name, value)
    if kind == "datetime":
        return _datetime(name, value)
    raise QueryCompileError(f"parameter {name} has no scalar binding for a {kind}")


def _declared(decl: QueryParamDeclaration, value: Any) -> _Bound:
    name = decl.name
    if decl.type == "daterange":
        return _daterange(name, value)
    if decl.type == "enum":
        return _Scalar(_enum(name, value, decl.enum_values))
    return _Scalar(_scalar_of_kind(name, value, decl.type))


def _inferred_scalar(name: str, value: Any) -> Scalar:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return _integer(name, value)
    if isinstance(value, float | Decimal):
        return _number(name, value)
    if isinstance(value, str):
        return _string(name, value)
    if isinstance(value, datetime | date):
        return _date(name, value)
    if isinstance(value, Mapping):
        raise QueryCompileError(
            f"parameter {name} is an object; only a declared daterange may be one"
        )
    if isinstance(value, list | tuple):
        raise QueryCompileError(f"parameter {name} may not nest a list inside a list")
    raise QueryCompileError(f"parameter {name} has a type that cannot be bound")


def _inferred(name: str, value: Any) -> _Bound:
    if value is None:
        return _Scalar(None)
    if isinstance(value, list | tuple):
        if not value:
            raise QueryCompileError(f"parameter {name} is an empty list (IN () is not valid SQL)")
        if len(value) > MAX_LIST_ITEMS:
            raise QueryCompileError(f"parameter {name} lists more than {MAX_LIST_ITEMS} items")
        items = tuple(_inferred_scalar(f"{name}[{i}]", item) for i, item in enumerate(value))
        if len({_kind_of(item) for item in items}) != 1:
            raise QueryCompileError(f"parameter {name} mixes value types in one list")
        return _List(items)
    return _Scalar(_inferred_scalar(name, value))


def _kind_of(value: Scalar) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"
    return "string"


# --------------------------------------------------------------------------- #
# placeholders
# --------------------------------------------------------------------------- #

_CLICKHOUSE_TYPES = {
    "string": "String",
    "integer": "Int64",
    "number": "Float64",
    "date": "Date",
    "datetime": "DateTime",
}

#: The ClickHouse placeholder type for a slot the template already typed in
#: Tinybird's grammar. ``Boolean`` is the one that does not carry over: it is
#: not a ClickHouse type, so the placeholder is a ``UInt8`` and the value is
#: sent as 1/0 — the same conversion an untyped boolean gets.
_TEMPLATE_CLICKHOUSE_TYPES = {
    "String": "String",
    "Int32": "Int32",
    "Int64": "Int64",
    "Float64": "Float64",
    "Date": "Date",
    "DateTime": "DateTime",
    "Boolean": "UInt8",
}

#: Tinybird's own template type functions, by the compiler's value kind. Every
#: one of them parses the form field back to that type on Tinybird's side and
#: REFUSES a value that is not of it — ``Error validating '1 OR 1=1' to type
#: Int64`` — so the engine re-checks, in its own grammar, what was typed here.
#: ``Boolean`` is the exception that must not be trusted to re-check: it reads
#: anything unrecognized as true, which is why the driver renders a bool as
#: exactly ``true``/``false`` rather than passing a caller's word through.
_TINYBIRD_TYPES = {
    "string": "String",
    "integer": "Int64",
    "number": "Float64",
    "date": "Date",
    "datetime": "DateTime",
    "boolean": "Boolean",
}


def _placeholder(
    style: PlaceholderStyle,
    name: str,
    value: Scalar,
    engine: str,
    template_type: str = "",
) -> tuple[str, Any]:
    """The placeholder text for one bound value, and the value as the driver
    should receive it.

    ``template_type`` is the Tinybird type function the slot was WRITTEN with,
    when it was written that way. It wins over the value's own kind on the two
    brace engines, so a statement's own typing survives the round trip: the
    text Tinybird receives is the text the author wrote, which is what lets it
    pass the driver's "only a typed slot may be a directive" check."""
    if style == "pyformat":
        return f"%({name})s", value
    if style == "dollar":
        return f"${name}", value
    if style == "colon":
        return f":{name}", value
    if value is None:
        raise QueryCompileError(
            f"a null cannot be bound on {engine}; compare with IS NULL in the template instead"
        )
    kind = _kind_of(value)
    if style == "tinybird":
        # Doubled braces: the outer pair is Tinybird's template delimiter, and
        # the value arrives beside the statement rather than inside it.
        return f"{{{{{template_type or _TINYBIRD_TYPES[kind]}({name})}}}}", value
    if template_type:
        clickhouse = _TEMPLATE_CLICKHOUSE_TYPES[template_type]
        return f"{{{name}:{clickhouse}}}", (1 if value else 0) if clickhouse == "UInt8" else value
    if kind == "boolean":
        return f"{{{name}:UInt8}}", 1 if value else 0
    return f"{{{name}:{_CLICKHOUSE_TYPES[kind]}}}", value


def compile_query(
    template: str,
    params: Mapping[str, Any],
    engine: str,
    *,
    declarations: Iterable[QueryParamDeclaration | Mapping[str, Any]] = (),
) -> BoundQuery:
    """``template`` with every slot replaced by ``engine``'s placeholder, plus
    the typed values to bind. :class:`QueryCompileError` for a missing,
    unknown or ill-typed value, a slot used the wrong way, or an engine with no
    bound-parameter path."""
    style = ENGINE_STYLES.get(engine.strip().lower())
    if style is None:
        raise QueryCompileError(f"bound parameters are not supported on {engine or 'this engine'}")
    if len(params) > MAX_PARAMS:
        raise QueryCompileError(f"more than {MAX_PARAMS} parameters")
    decls: dict[str, QueryParamDeclaration] = {}
    for raw in declarations:
        decl = (
            raw
            if isinstance(raw, QueryParamDeclaration)
            else QueryParamDeclaration.from_mapping(raw)
        )
        if decl.name in decls:
            raise QueryCompileError(f"parameter {decl.name} is declared twice")
        decls[decl.name] = decl
    slots = slots_of(template)
    names = tuple(slot.name for slot in slots)
    # A slot written in Tinybird's grammar carries its own declaration. It is
    # used when the object declared nothing for that name — a declaration is
    # the richer vocabulary (an enum, a required flag) and stays in charge of
    # the VALUE — and it always names the type the placeholder is written back
    # out with, so the statement keeps the typing its author gave it.
    typed = {slot.name: slot.type for slot in slots if slot.template_type}
    for key in params:
        if not isinstance(key, str) or not _NAME.match(key):
            raise QueryCompileError(f"invalid parameter name {key!r}")
    unknown = sorted(key for key in params if key not in names and key not in decls)
    if unknown:
        raise QueryCompileError(f"no placeholder named {', '.join(unknown)}")
    missing = [
        name for name in names if name not in params and (name not in decls or decls[name].required)
    ]
    if missing:
        raise QueryCompileError(f"missing value for {', '.join(missing)}")
    bound: dict[str, _Bound] = {}
    for name in names:
        declared = decls.get(name)
        if name not in params:
            bound[name] = _Scalar(None)
        elif declared is not None:
            bound[name] = _declared(declared, params[name])
        elif name in typed:
            bound[name] = _Scalar(_scalar_of_kind(name, params[name], typed[name]))
        else:
            bound[name] = _inferred(name, params[name])
    values: dict[str, Any] = {}
    owner: dict[str, str] = {}

    def bind(slot: str, name: str, value: Scalar, template_type: str) -> str:
        # A slot may repeat (``{a} = {a}``); two different slots may not compile
        # to one name (``{ids}`` expands to ``ids_0``, which a slot of that name
        # would overwrite).
        if name in values and owner[name] != slot:
            raise QueryCompileError(f"compiled parameter name {name} is used twice")
        text, driver_value = _placeholder(style, name, value, engine, template_type)
        values[name] = driver_value
        owner[name] = slot
        return text

    def render(match: re.Match[str], name: str) -> str:
        part = match.group("part")
        type_text = match.group("type") or ""
        item = bound[name]
        if isinstance(item, _DateRange):
            if part is None:
                raise QueryCompileError(
                    f"{{{name}}} is a daterange: use {{{name}.start}} and {{{name}.end}}"
                )
            end = item.start if part == "start" else item.end
            return bind(name, f"{name}__{part}", end, type_text)
        if part is not None:
            raise QueryCompileError(f"{{{name}.{part}}} needs a daterange, and {name} is not one")
        if isinstance(item, _List):
            return (
                "("
                + ", ".join(
                    bind(name, f"{name}_{i}", v, type_text) for i, v in enumerate(item.values)
                )
                + ")"
            )
        return bind(name, name, item.value, type_text)

    escape = bool(names)
    pieces: list[str] = []
    cursor = 0
    for match in SLOT.finditer(template):
        slot_name = _slot_name(match)
        if slot_name is None:
            # A doubled-brace group that is not a slot: it stays where it is,
            # inside the literal text — which on Tinybird is exactly where the
            # directive refusal below catches it.
            continue
        pieces.append(_literal(template[cursor : match.start()], style, escape=escape))
        pieces.append(render(match, slot_name))
        cursor = match.end()
    pieces.append(_literal(template[cursor:], style, escape=escape))
    return BoundQuery(sql="".join(pieces), params=values, engine=engine, placeholders=names)


def _literal(text: str, style: PlaceholderStyle, *, escape: bool) -> str:
    """Template text between slots. Once pyformat placeholders are bound, a
    literal ``%`` (a LIKE pattern) has to be doubled or the driver reads it as
    a directive; nothing else is touched.

    On Tinybird the statement is sent as a TEMPLATE once it carries a slot, so
    the author's own ``{{…}}`` / ``{%…%}`` would be read by Tinybird's engine
    too — and its other directives (``sql_and``, ``columns``, a bare
    ``{{name}}``) write text into the SQL rather than binding beside it. This
    is the text BETWEEN the slots, so a typed ``{{Date(d)}}`` has already been
    taken as a slot and is not seen here: the exemption is exactly the slots
    the compiler itself emits, and every other directive is refused. The driver
    checks the same thing again on the wire."""
    if style == "pyformat" and escape:
        return text.replace("%", "%%")
    if style == "tinybird" and escape:
        found = next((d for d in ("{{", "{%") if d in text), None)
        if found is not None:
            raise QueryCompileError(
                f"a Tinybird template may not contain {found!r}: a parametrised statement is "
                "sent as a template, and only a {name} slot may become a directive"
            )
    return text


__all__ = [
    "ENGINE_STYLES",
    "MAX_LIST_ITEMS",
    "MAX_PARAMS",
    "MAX_STRING_CHARS",
    "PARAM_TYPES",
    "SLOT",
    "TEMPLATE_SLOT_TYPES",
    "BoundQuery",
    "ParamType",
    "PlaceholderStyle",
    "QueryCompileError",
    "QueryParamDeclaration",
    "QuerySlot",
    "Scalar",
    "compile_query",
    "placeholders_of",
    "slots_of",
]
