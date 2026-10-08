"""Generate the golden renderings of every row-backed object type.

The goldens are a wire format: a chat exported today must still open in a reader
built a year from now, so the bytes are pinned and a change to them is a change
to the format, reviewed as one. Regenerate with::

    uv run python packages/api-core/tests/fixtures/files/generate_renderings.py

Each object type has a committed *input* (``<type>.input.json``, plain data —
exactly what the renderer's query would return) and a committed *rendering*
(``<type>.<extension>``). Rendering here runs the same pure functions the
database-backed renderers call, which is what lets the determinism test render
in two subprocesses without a database and still prove the bytes the product
serves.

``--emit <type>`` writes one rendering to stdout, byte for byte. That is the
determinism test's subprocess entry point.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

from alkera_core.files.providers.rows import (
    RENDERING_SCHEMA_VERSION,
    render_chat,
    render_query,
    render_result,
)

HERE: Final = Path(__file__).resolve().parent
RENDERINGS: Final = HERE / "renderings"

#: The extension each type's rendering carries, mirroring the renderers'.
EXTENSIONS: Final[dict[str, str]] = {"chat": ".json", "query": ".json", "result": ".csv"}

#: A fixed object header per type. The ids are constants, not fresh UUIDs: a
#: golden whose content changed every run would prove nothing about determinism.
_CHAT_OBJECT: Final[dict[str, Any]] = {
    "id": "3f1c0a5e-0000-4000-8000-00000000c0a7",
    "logical_id": "weekly-review",
    "namespace": "workspace",
    "title": "Weekly review",
    "version": 4,
}
_QUERY_OBJECT: Final[dict[str, Any]] = {
    "id": "3f1c0a5e-0000-4000-8000-00000000d1e2",
    "logical_id": "top-accounts",
    "namespace": "workspace",
    "title": "Top accounts",
    "version": 2,
}
_RESULT_OBJECT: Final[dict[str, Any]] = {
    "id": "3f1c0a5e-0000-4000-8000-00000000f00d",
    "logical_id": "top-accounts-run-7",
    "namespace": "workspace",
    "title": "Top accounts, run 7",
    "version": 1,
}

#: The chat transcript, with the cases a canonical rendering has to survive:
#: keys out of alphabetical order, a non-ASCII title, an embedded newline, a
#: float, a null and a nested object.
CHAT_INPUT: Final[dict[str, Any]] = {
    "schema_version": RENDERING_SCHEMA_VERSION,
    "type": "chat",
    "object": _CHAT_OBJECT,
    "messages": [
        {
            "seq": 1,
            "role": "user",
            "kind": "prompt",
            "event_id": "evt-1",
            "payload": {"text": "résumé the quarter\nin two lines", "zeta": 1, "alpha": 2},
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "message",
            "event_id": "evt-2",
            "payload": {"text": "Done.", "usage": {"cost_usd": 0.125, "tokens": 4096}},
        },
        {
            "seq": 3,
            "role": "tool",
            "kind": "tool_result",
            "event_id": "evt-3",
            "payload": {"tool": "sql", "rows": 12, "error": None},
        },
    ],
}

#: The saved query's spec, as the writable rendering carries it.
QUERY_INPUT: Final[dict[str, Any]] = {
    "schema_version": RENDERING_SCHEMA_VERSION,
    "type": "query",
    "object": _QUERY_OBJECT,
    "spec": {
        "sql": "select account, revenue\nfrom accounts\norder by revenue desc",
        "connection_id": "3f1c0a5e-0000-4000-8000-0000000000c1",
        "parameters": {"limit": 10, "since": "2026-01-01"},
        "schema_version": "1.0.0",
    },
}

#: The result's rows, with every cell shape RFC 4180 has an opinion about: a
#: comma, an embedded quote, a newline, a control character, a null, a bool and
#: a float. NUL is not among them because Postgres refuses it in ``jsonb``, so a
#: result can never carry one; the renderer's own quoting of it is pinned in the
#: unit case instead.
RESULT_INPUT: Final[dict[str, Any]] = {
    "columns": ["account", "revenue", "active", "note"],
    "rows": [
        ["Acme, Inc.", 1200.5, True, 'he said "hi"'],
        ["Beta\nWorks", 0, False, None],
        ["Gamma\x01Ltd", -3, None, "plain"],
    ],
}

INPUTS: Final[dict[str, dict[str, Any]]] = {
    "chat": CHAT_INPUT,
    "query": QUERY_INPUT,
    "result": RESULT_INPUT,
}


def render_sample(object_type: str) -> bytes:
    """The golden bytes for ``object_type``, from its committed input."""
    document = INPUTS[object_type]
    if object_type == "chat":
        return render_chat(document)
    if object_type == "query":
        return render_query(document)
    if object_type == "result":
        return render_result(document["columns"], document["rows"])
    raise KeyError(object_type)


def golden_path(object_type: str) -> Path:
    return RENDERINGS / f"{object_type}{EXTENSIONS[object_type]}"


def input_path(object_type: str) -> Path:
    return RENDERINGS / f"{object_type}.input.json"


def write_all() -> list[Path]:
    RENDERINGS.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for object_type in INPUTS:
        source = input_path(object_type)
        source.write_text(
            json.dumps(INPUTS[object_type], indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        target = golden_path(object_type)
        target.write_bytes(render_sample(object_type))
        written.extend((source, target))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit", choices=sorted(INPUTS), help="write one rendering to stdout")
    args = parser.parse_args(argv)
    if args.emit:
        sys.stdout.buffer.write(render_sample(args.emit))
        return 0
    for written in write_all():
        print(written)
    return 0


if __name__ == "__main__":  # pragma: no cover - a script entry point
    raise SystemExit(main())
