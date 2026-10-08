"""The settings block: a TOML document fenced in comments before ``import marimo``.

```
# >>> alkera
# format = "1.0"
# reactivity = "lazy"
# <<< alkera
```

The fence is not a PEP 723 block (it does not match PEP 723's expression), so
tools that do not know it see comments and keep it. Writers place it after a
shebang, a PEP 263 coding line and a PEP 723 ``script`` block when those are
present, and first otherwise. Readers accept it anywhere before
``import marimo``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import tomlkit
import tomlkit.exceptions

from alkera_notebook.format.ir import FORMAT_VERSION, Violation
from alkera_notebook.format.settings import NOTEBOOK_SETTINGS, SettingSpec, default_settings

FENCE_OPEN = "# >>> alkera"
FENCE_CLOSE = "# <<< alkera"

_CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-_.a-zA-Z0-9]+")
_PEP723_OPEN_RE = re.compile(r"^# /// [a-zA-Z0-9-]+$")
_PEP723_CLOSE = "# ///"
_FORMAT_RE = re.compile(r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$")


SETTINGS: tuple[SettingSpec, ...] = NOTEBOOK_SETTINGS
"""Known keys after ``format``, in the order writers emit them."""

KNOWN_KEYS = frozenset({"format"} | {s.name for s in SETTINGS})


@dataclass
class HeaderParts:
    """The result of splitting the text before ``import marimo``."""

    header_text: str
    fence_body: str | None
    fence_line: int
    violations: list[Violation] = field(default_factory=list)


def split_header(region: str, first_line: int = 1) -> HeaderParts:
    """Remove the first well-formed settings fence from ``region``.

    ``region`` is the text before ``import marimo``; ``first_line`` is the
    1-based file line of its first line, for violations. The returned
    ``header_text`` is the region without the fence lines and without trailing
    blank lines (ending in a newline when it is not empty).
    """
    lines = region.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    violations: list[Violation] = []
    fence: tuple[int, int] | None = None
    index = 0
    while index < len(lines):
        if lines[index] != FENCE_OPEN:
            index += 1
            continue
        close = None
        bad = None
        for j in range(index + 1, len(lines)):
            if lines[j] == FENCE_CLOSE:
                close = j
                break
            if not lines[j].startswith("#"):
                bad = j
                break
        if bad is not None:
            violations.append(
                Violation(
                    "settings_line",
                    first_line + bad,
                    "a settings block line is not a comment; the block is ignored",
                )
            )
        elif close is None:
            violations.append(
                Violation(
                    "settings_unclosed",
                    first_line + index,
                    f"`{FENCE_OPEN}` has no `{FENCE_CLOSE}`; the block is ignored",
                )
            )
        elif fence is None:
            fence = (index, close)
            index = close + 1
            continue
        else:
            violations.append(
                Violation(
                    "settings_duplicate",
                    first_line + index,
                    "a second settings block is kept as comments and not read",
                )
            )
        index += 1
    body: str | None = None
    kept = lines
    fence_line = 0
    if fence is not None:
        start, end = fence
        fence_line = first_line + start
        body_lines: list[str] = []
        for offset, line in enumerate(lines[start + 1 : end], start=start + 1):
            if line == "#":
                body_lines.append("")
            elif line.startswith("# "):
                body_lines.append(line[2:])
            else:
                body_lines.append(line[1:])
                violations.append(
                    Violation(
                        "settings_line",
                        first_line + offset,
                        "settings lines start with `# `",
                    )
                )
        body = "\n".join(body_lines)
        kept = lines[:start] + lines[end + 1 :]
    while kept and not kept[-1].strip():
        kept.pop()
    text = "\n".join(kept) + "\n" if kept else ""
    return HeaderParts(text, body, fence_line, violations)


@dataclass
class Settings:
    format: str
    values: dict[str, Any]
    unknown: str
    violations: list[Violation]
    #: The known keys the fence itself sets to a valid value; ``values``
    #: fills every other key with its default.
    set_keys: frozenset[str] = frozenset()


def _plain(value: Any) -> Any:
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value


def parse_settings(body: str | None, line: int = 0) -> Settings:
    """Read the TOML body of the fence. Never raises.

    Known keys are validated (a wrong type is a violation and the default
    applies); unknown keys are returned as TOML text, in their original order
    and spelling. When the body is not valid TOML, each line is read on its
    own: lines holding a single known key count, everything else is kept as
    unknown text so it is written back unchanged.
    """
    violations: list[Violation] = []
    values = default_settings()
    if body is None:
        return Settings(FORMAT_VERSION, values, "", violations)
    found: dict[str, Any] = {}
    unknown: str
    try:
        document = tomlkit.parse(body)
    except tomlkit.exceptions.TOMLKitError as error:
        violations.append(Violation("settings_toml", line, f"settings are not valid TOML: {error}"))
        kept: list[str] = []
        for text in body.split("\n"):
            try:
                single = tomlkit.parse(text)
            except tomlkit.exceptions.TOMLKitError:
                kept.append(text)
                continue
            keys = list(single.keys())
            if len(keys) == 1 and keys[0] in KNOWN_KEYS and keys[0] not in found:
                found[keys[0]] = _plain(single[keys[0]])
            else:
                kept.append(text)
        unknown = "\n".join(kept)
    else:
        for key in list(document.keys()):
            if key in KNOWN_KEYS:
                found[key] = _plain(document[key])
                del document[key]
        unknown = document.as_string()
    unknown = unknown.strip("\n")
    if unknown and not unknown.strip():
        unknown = ""

    set_keys: set[str] = set()
    fmt = FORMAT_VERSION
    if "format" in found:
        value = found["format"]
        if isinstance(value, str) and _FORMAT_RE.fullmatch(value):
            fmt = value
        else:
            violations.append(
                Violation("format_version", line, f'`format` must be "MAJOR.MINOR", got {value!r}')
            )
    for setting in SETTINGS:
        if setting.name not in found:
            continue
        value = found[setting.name]
        if setting.valid(value):
            values[setting.name] = value
            set_keys.add(setting.name)
        else:
            violations.append(
                Violation(
                    "settings_value",
                    line,
                    f"`{setting.name}` must be {setting.expected}, got {value!r}; default used",
                )
            )
    return Settings(fmt, values, unknown, violations, frozenset(set_keys))


def _toml_value(value: Any) -> str:
    return str(tomlkit.item(value).as_string())


def render_fence(fmt: str, settings: Mapping[str, Any], unknown: str) -> str:
    """The fence text (ending in a newline): known keys in table order with
    defaults omitted (``format`` always written), then the unknown text."""
    body = [f"format = {_toml_value(fmt)}"]
    for setting in SETTINGS:
        if setting.name not in settings:
            continue
        value = settings[setting.name]
        if value is None or value == setting.default or not setting.valid(value):
            continue
        body.append(f"{setting.name} = {_toml_value(value)}")
    if unknown.strip():
        body.extend(unknown.strip("\n").split("\n"))
    lines = [FENCE_OPEN, *("# " + text if text else "#" for text in body), FENCE_CLOSE]
    return "\n".join(lines) + "\n"


def fence_position(lines: list[str]) -> int:
    """Where a writer puts the fence among the header's lines."""
    index = 0
    if lines and lines[0].startswith("#!"):
        index = 1
    if index < len(lines) and index < 2 and _CODING_RE.match(lines[index]):
        index += 1
    if index < len(lines) and _PEP723_OPEN_RE.match(lines[index]):
        for j in range(index + 1, len(lines)):
            if lines[j] == _PEP723_CLOSE:
                return j + 1
            if not (lines[j] == "#" or lines[j].startswith("# ")):
                break
    return index


def assemble_header(header_text: str, fence: str) -> str:
    """``header_text`` with ``fence`` inserted at the canonical position,
    followed by one blank line (the text that precedes ``import marimo``)."""
    lines = header_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    position = fence_position(lines)
    fence_lines = fence.rstrip("\n").split("\n")
    out = lines[:position] + fence_lines + lines[position:]
    return "\n".join(out) + "\n\n"
