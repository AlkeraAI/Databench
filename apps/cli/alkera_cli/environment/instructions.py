"""What an agent starting in a workspace is told about its environment.

Two sources, both bounded: the human-written ``ENVIRONMENT.md`` (how to run
things, which environment to use, conventions) and a summary generated from
``.alkera-environment.json``. A session's system prompt carries the block (see
``harness.session_instructions``), so it holds on every turn of every backend.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alkera_cli.environment.files import read_spec, read_workspace_text
from alkera_cli.environment.spec import (
    INSTRUCTIONS_FILENAME,
    SPEC_FILENAME,
    EnvironmentSpec,
)

#: The agent tools that keep the spec current, spelled once.
CAPTURE_TOOL_NAME = "environment.capture"
RECREATE_TOOL_NAME = "environment.recreate"

#: How much of ENVIRONMENT.md reaches the prompt; the rest is one read away.
MAX_INSTRUCTIONS_CHARS = 6000
#: The largest ENVIRONMENT.md read at all.
MAX_INSTRUCTIONS_FILE_BYTES = 256 * 1024
#: How many names each list in the summary shows, and the summary's length.
MAX_LISTED = 20
MAX_SUMMARY_CHARS = 3000

_NOT_PORTABLE_WORDS: dict[str, str] = {
    "editable_outside_workspace": "editable install outside the workspace",
    "path_outside_workspace": "installed from a folder outside the workspace",
    "path_entry_outside_workspace": "sys.path folder outside the workspace",
    "system_package": "installed by the operating system",
    "system_site_packages": "the environment also reads the system site-packages",
    "system_interpreter": "captured from a system interpreter, not a virtual environment",
    "conda_package_without_url": "conda package with no download URL",
    "index_credentials_removed": "index credentials were not captured",
    "unsupported_value": "a name or path no recreate can use safely",
}

TOOL_SENTENCE = (
    f"After you install or remove packages, call `{CAPTURE_TOOL_NAME}` so the next agent or "
    f"machine gets the same environment. `{RECREATE_TOOL_NAME}` builds an environment from "
    f"the spec; call it with `dry_run` first to see the plan."
)


def _listed(items: list[str]) -> str:
    shown = items[:MAX_LISTED]
    more = len(items) - len(shown)
    text = ", ".join(shown)
    return f"{text} and {more} more" if more else text


def _cap(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    return f"{cut}\n\n[{INSTRUCTIONS_FILENAME} continues; read the file for the rest.]"


def summarize_spec(spec: EnvironmentSpec) -> str:
    """One paragraph a model reads: what the environment is and holds."""
    kinds = {
        "venv": "a virtual environment",
        "conda": "a conda environment",
        "system": "a system interpreter",
        "none": "project files only",
    }
    when = spec.captured_at or "at an unknown time"
    kind = kinds.get(spec.env_kind, "an environment")
    parts = [f"The captured environment ({SPEC_FILENAME}, captured {when}) is {kind}"]
    if spec.python and spec.python.version:
        impl = f" ({spec.python.implementation})" if spec.python.implementation else ""
        parts[0] += f" on Python {spec.python.version}{impl}"
    if spec.platform and spec.platform.sys_platform:
        parts[0] += f" for {spec.platform.sys_platform} {spec.platform.machine}".rstrip()
    count = len(spec.packages)
    parts[0] += f" with {count} package." if count == 1 else f" with {count} packages."
    requested = [
        f"{p.name} {p.version}".strip()
        for p in spec.packages
        if p.requested and p.source != "editable"
    ]
    if requested:
        parts.append(f"Installed directly: {_listed(requested)}.")
    editable = [f"{p.name} ({p.path})" for p in spec.packages if p.source == "editable"]
    if editable:
        parts.append(f"Editable from this workspace: {_listed(editable)}.")
    if spec.path_entries:
        parts.append(f"Workspace folders on sys.path: {_listed(spec.path_entries)}.")
    if spec.conda and (spec.conda.specs or spec.conda.packages):
        names = spec.conda.specs or [p.name for p in spec.conda.packages]
        parts.append(f"Conda specs: {_listed(names)}.")
    issues = [
        f"{i.name} ({_NOT_PORTABLE_WORDS.get(i.kind, i.kind)})"
        if i.name
        else _NOT_PORTABLE_WORDS.get(i.kind, i.kind)
        for i in spec.not_portable
    ]
    if issues:
        parts.append(f"Not portable: {_listed(issues)}.")
    text = " ".join(parts)
    return text if len(text) <= MAX_SUMMARY_CHARS else text[: MAX_SUMMARY_CHARS - 1] + "…"


def render_environment_instructions(root: Path, *, tools: bool) -> str:
    """The environment block for a session whose workspace is ``root``, or
    ``""`` when the workspace has neither ``ENVIRONMENT.md`` nor a spec.
    ``tools`` says the session serves the capture and recreate tools."""
    sections: list[str] = []
    written = read_workspace_text(
        root, INSTRUCTIONS_FILENAME, max_bytes=MAX_INSTRUCTIONS_FILE_BYTES
    )
    if written and written.strip():
        sections.append(
            f"{INSTRUCTIONS_FILENAME}, written by the people who work in this workspace:\n\n"
            + _cap(written, MAX_INSTRUCTIONS_CHARS)
        )
    spec = read_spec(root)
    if spec is not None:
        sections.append(summarize_spec(spec))
    # A workspace with neither file gets no block at all, so a session that
    # starts where nothing was captured reads the same prompt as before.
    if not sections:
        return ""
    if tools:
        sections.append(TOOL_SENTENCE)
    return "## Workspace environment\n\n" + "\n\n".join(sections)


async def environment_blocks(root: Path, *, tools: bool) -> list[str]:
    """The environment block for a session's system prompt, as a list to extend
    the session's blocks with (empty when the workspace has nothing to say).
    Read off the event loop."""
    block = await asyncio.to_thread(render_environment_instructions, root, tools=tools)
    return [block] if block else []


__all__ = [
    "CAPTURE_TOOL_NAME",
    "MAX_INSTRUCTIONS_CHARS",
    "RECREATE_TOOL_NAME",
    "TOOL_SENTENCE",
    "environment_blocks",
    "render_environment_instructions",
    "summarize_spec",
]
