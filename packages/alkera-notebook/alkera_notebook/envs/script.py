"""PEP 723 ``script`` blocks: reading them and adding dependencies.

The ``script`` environment's spec is the notebook's own PEP 723 block, so it
lives in the document. Installing into it is a document edit: the engine
calls :func:`add_script_dependencies` on the notebook's header text and
applies the result as a document op; nothing here writes the notebook.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from typing import Any

# PEP 723's reference expression.
SCRIPT_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def find_script_block(text: str) -> re.Match[str] | None:
    """The ``script`` block of ``text`` (the first one), or None."""
    for m in SCRIPT_BLOCK.finditer(text):
        if m.group("type") == "script":
            return m
    return None


def script_block_text(text: str) -> str | None:
    """The ``script`` block, fences included, exactly as written."""
    m = find_script_block(text)
    return m.group(0) if m else None


def _content_toml(content: str) -> str:
    return "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in content.splitlines(keepends=True)
    )


def parse_script_block(text: str) -> dict[str, Any] | None:
    """The block's TOML, or None when there is no block or it does not parse."""
    m = find_script_block(text)
    if m is None:
        return None
    try:
        return tomllib.loads(_content_toml(m.group("content")))
    except tomllib.TOMLDecodeError:
        return None


def script_dependencies(text: str) -> list[str]:
    data = parse_script_block(text) or {}
    deps = data.get("dependencies", [])
    return [d for d in deps if isinstance(d, str)] if isinstance(deps, list) else []


def canonical_name(requirement: str) -> str:
    m = _NAME.match(requirement)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else requirement.lower()


def _deps_lines(deps: Sequence[str]) -> list[str]:
    if not deps:
        return ["# dependencies = []"]
    return ["# dependencies = ["] + [f'#     "{d}",' for d in deps] + ["# ]"]


def add_script_dependencies(header_text: str, packages: Sequence[str]) -> str:
    """``header_text`` with ``packages`` added to its PEP 723 ``dependencies``.

    A package already listed (by canonical name) is replaced by the new
    requirement. Other keys and lines of the block are kept as written. With
    no block, one is inserted after a shebang and coding line if present.
    Raises ``ValueError`` when an existing block does not parse.
    """
    m = find_script_block(header_text)
    if m is None:
        lines = header_text.splitlines()
        keep = 0
        if lines and lines[0].startswith("#!"):
            keep = 1
        if len(lines) > keep and re.match(r"^#.*coding[:=]", lines[keep]):
            keep += 1
        block = ["# /// script", *_deps_lines(list(packages)), "# ///"]
        if len(lines) > keep and lines[keep].startswith("#"):
            # Keep the new block apart from a comment (or another block) below it.
            block.append("")
        out = lines[:keep] + block + lines[keep:]
        return "\n".join(out) + ("\n" if header_text.endswith("\n") or not header_text else "")
    new_names = {canonical_name(p) for p in packages}
    existing = _existing_dependencies(header_text)
    deps = [d for d in existing if canonical_name(d) not in new_names] + list(packages)
    return _with_dependencies(header_text, m, deps)


def remove_script_dependencies(header_text: str, names: Sequence[str]) -> str:
    """``header_text`` with every requirement for ``names`` (compared by
    canonical name) taken out of its PEP 723 ``dependencies``; unchanged when
    it has no block. Raises ``ValueError`` when the block does not parse."""
    m = find_script_block(header_text)
    if m is None:
        return header_text
    gone = {canonical_name(n) for n in names}
    deps = [d for d in _existing_dependencies(header_text) if canonical_name(d) not in gone]
    return _with_dependencies(header_text, m, deps)


def _existing_dependencies(header_text: str) -> list[str]:
    data = parse_script_block(header_text)
    if data is None:
        raise ValueError("the script block is not valid TOML")
    return [d for d in data.get("dependencies", []) if isinstance(d, str)]


def _with_dependencies(header_text: str, m: re.Match[str], deps: list[str]) -> str:
    """The block ``m`` matched, with its ``dependencies`` set to ``deps`` and
    every other line kept as written."""
    content_lines = m.group("content").splitlines()
    out_lines: list[str] = []
    i = 0
    replaced = False
    while i < len(content_lines):
        line = content_lines[i]
        body = line[2:] if line.startswith("# ") else line[1:]
        if not replaced and re.match(r"^dependencies\s*=", body):
            depth = body.count("[") - body.count("]")
            while depth > 0 and i + 1 < len(content_lines):
                i += 1
                nxt = content_lines[i]
                nbody = nxt[2:] if nxt.startswith("# ") else nxt[1:]
                depth += nbody.count("[") - nbody.count("]")
            out_lines += _deps_lines(deps)
            replaced = True
        else:
            out_lines.append(line)
        i += 1
    if not replaced:
        out_lines += _deps_lines(deps)
    new_block = "# /// script\n" + "\n".join(out_lines) + "\n# ///"
    return header_text[: m.start()] + new_block + header_text[m.end() :]
