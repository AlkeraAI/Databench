"""The members a row-backed FOLDER holds whose bytes are derived, never stored.

A chat template materializes as a folder: ``scratch/`` holds the files a new
chat starts with, and ``README.md`` says what the template is for. The README
is not a file anybody wrote. It is rendered from the template's row on every
read, by the one function a mount, a pull and the drive all call, so editing
the brief changes the folder with no stored bytes to rewrite or drift.

The rendering is pure (plain data in, bytes out), so the bytes are identical
in a mount, in a pull and in a test, and nothing here needs a session.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "CONTEXT_FOLDER_TYPES",
    "DERIVED_MEMBERS",
    "render_member",
]

#: Every derived member, as ``{object type: {member name: mime}}``. One
#: registry: the bridge creates exactly these children, the provider registers
#: exactly these renderers, and a new member is a line here rather than a line
#: in each.
DERIVED_MEMBERS: Final[Mapping[str, Mapping[str, str]]] = {
    "chat_template": {"README.md": "text/markdown"},
}

#: The object types that materialize as a replication-context folder. Empty:
#: saved queries and reports are retired in favour of chat templates. Exported
#: so a caller that asks is answered "none" rather than failing to import.
CONTEXT_FOLDER_TYPES: Final[frozenset[str]] = frozenset()


def render_member(object_type: str, member: str, document: Mapping[str, Any]) -> bytes:
    """The bytes of one derived member, from the object's stamped envelope.

    ``document`` is what the rows renderer produces (``{"type", "object",
    "spec"}``), so the member and the object's own rendering can never describe
    different rows.
    """
    members = DERIVED_MEMBERS.get(object_type)
    if members is None or member not in members:
        raise KeyError(f"{object_type!r} has no derived member {member!r}")
    return _render_template_readme(document).encode("utf-8")


def _render_template_readme(document: Mapping[str, Any]) -> str:
    """What a person opening a chat template's folder reads.

    Short on purpose: the brief is the template author's own words and is
    reproduced verbatim, with only enough around it to say what the folder is
    and where the files a new chat starts with live.
    """
    spec = _mapping(document.get("spec"))
    header = _mapping(document.get("object"))
    title = str(spec.get("title") or header.get("title") or "Untitled")
    brief = str(spec.get("brief") or "")
    return (
        f"# {title}\n"
        "\n"
        "This folder is an Alkera chat template. Start a new chat from it "
        "(double-click it in Files, or New from template) and the agent is handed "
        "the brief below with these files already in its working directory.\n"
        "\n"
        "## Brief\n"
        "\n"
        f"{brief}\n"
        "\n"
        "## Files\n"
        "\n"
        "The files a new chat starts with are in `scratch/`.\n"
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
