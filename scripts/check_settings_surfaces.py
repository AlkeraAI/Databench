"""Keep every runtime setting reachable from the surfaces an operator edits.

A setting that exists only in `config.py` ships its Alkera-hosted default to
every self-hosted install, and the operator never learns there was a number to
pick. That is how a 1200 MiB upload budget written for a 4 GiB ECS task lands
inside a 512Mi Helm pod: nothing in the chart, compose, the ECS task env, the
deploy notes or `.env.example` ever mentioned it.

This checker closes the gap by construction. It walks `Settings.model_fields`,
resolves each field to the environment variable that sets it, and reports which
operator surfaces mention that name. ``.env.example`` is the committed registry
every install starts from; every other surface is declared by the tree that owns
it, in a ``settings-surfaces.toml`` beside it (see ``load_surfaces``): a
deployment surface renders values onto containers (compose, a chart, task
definitions), a docs surface tells an operator what a number bounds.

Settings are declared in sections, each owned by the code that reads them: the
open platform's ``Settings`` and any section a distribution adds beside its own
code. A ``[[settings]]`` entry in a tree's ``settings-surfaces.toml`` names a
section and the registry it belongs to (``.env.example`` for the open one), so
each tree's example carries only the settings that tree declares.

The rules, each failing in both directions so the lists cannot rot into
permanent allowlists:

0. **One owner per setting, one registry per owner.** A name declared by two
   sections fails, and a registry that assigns a setting another registry owns
   fails: a product setting never leaks into the open example.
1. **Every setting is in its registry**, unless it is named in `DEV_ONLY` with
   a written reason. A `DEV_ONLY` entry that IS in `.env.example`, or that names
   a field that no longer exists, is itself a failure.
2. **Every setting in `OPERATOR_TUNED` reaches the operator docs and at least one
   deployment surface**, where the checkout holds them. These are the numbers a
   deployment has to pick rather than inherit: capacity budgets, edge timeouts,
   retention windows. An entry naming a field that no longer exists fails too.
3. **Every setting in `EVERY_DEPLOYMENT` is rendered by EVERY deployment
   surface**: the secrets and switches a production process refuses to boot
   over, where one surface forgetting the name ships an install that
   crash-loops. A stale entry fails here too.

A surface counts only where this checkout declares and holds it. Run from a
checkout that composes this tree (from its root, with this tree under it), the
checker reads that checkout's declarations too, so every surface either side
declares is held. A checkout with no deployment surface at all is itself a
failure.

A deployment surface counts only where it RENDERS the setting onto a container
(`NAME: value`, `NAME = value`, `- NAME=value`, `- name: NAME`), never where it
merely names it in a comment: a name that survives only in prose reaches no
running process, which is the gap this checker exists to close. `.env.example`
is stricter still; it must ASSIGN the name. The deploy notes are prose by
design, so a mention is what they are asked for.

Usage::

    python scripts/check_settings_surfaces.py            # enforce (exit 1 on a gap)
    python scripts/check_settings_surfaces.py --report   # print the full matrix
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel
from pydantic.fields import FieldInfo

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the field definitions (and their comments) are authored.
CONFIG_SOURCE = REPO_ROOT / "packages/api-core/alkera_core/config.py"

#: Settings deliberately absent from `.env.example`, each with the reason a
#: reader of that file would otherwise ask for. A setting is listed here because
#: naming it in the committed registry would be misleading or harmful, never
#: merely because writing the line was inconvenient.
DEV_ONLY: dict[str, str] = {
    "realtime_crdt_unsaved_sweep_enabled": (
        "A test-suite switch: a deployment always runs the sweep that writes back "
        "a stopped process's edits, and turning it off there would lose them."
    ),
    "aws_access_key_id": (
        "Static AWS keys are a `.env.local` affordance for local Bedrock. Every "
        "deployed shape uses the task/pod role, and a committed name invites a paste."
    ),
    "aws_secret_access_key": "Pairs with AWS_ACCESS_KEY_ID; same reason.",
    "aws_session_token": "Pairs with AWS_ACCESS_KEY_ID; same reason.",
    "oauth_mock_enabled": (
        "A test and local-development switch: it puts a mock sign-in button on the "
        "login page. The suite turns it on in conftest.py; nothing shipped may."
    ),
    "compose_project_name": (
        "Written per worktree by ops/scripts/workspace-env.sh into .env.workspace; "
        "it scopes local developer boxes and has no meaning in a deployment."
    ),
}

#: Settings a deployment has to CHOOSE rather than inherit: capacity budgets that
#: must fit the container, timeouts that must agree with the edge, and retention
#: windows that decide how much storage the install pays for. Each must be
#: documented in the operator docs and rendered by at least one deployment
#: surface, so an operator meets it before it meets them.
OPERATOR_TUNED: dict[str, str] = {
    # Capacity: must be sized against the container's memory limit.
    "files_upload_resident_budget_bytes": (
        "Per-process upload residency; OOMKills the container when it exceeds the memory limit."
    ),
    "files_max_upload_parts": "How many parts one session holds; the budget is sized on it.",
    "realtime_ws_max_connections": (
        "Peak websocket frame buffers per process = this times the launch line's --ws-max-size."
    ),
    "realtime_ws_frame_budget_bytes": (
        "The memory that peak is allowed to reach; the boot refuses a cap whose product exceeds it."
    ),
    # Edge agreement: wrong values cut live streams or hang a socket.
    "gateway_max_stream_seconds": "The ceiling a deploy's drain window is sized against.",
    "gateway_edge_idle_timeout_seconds": (
        "Must equal the deployment's own load-balancer idle timeout."
    ),
    "gateway_client_keepalive_seconds": (
        "Must stay below the edge idle timeout or an idle stream is dropped."
    ),
    "health_ready_timeout_seconds": "Must stay below the load balancer's health-check timeout.",
    # Schema upgrades: how long a replica waits before it gives up on one.
    "migration_runner_wait_seconds": (
        "Every replica runs the upgrade at once; all but one wait this long or fail to start."
    ),
    # Storage retention: decides the storage bill.
    "files_version_keep_newest": "How many versions of a file are retained.",
    "files_version_keep_window_days": "How long older versions are retained.",
    "files_lease_ttl_seconds": (
        "How long a folder's write fence outlives the holder's last heartbeat."
    ),
    # Master switches an operator turns off.
    "files_enabled": "Turns the whole Files plane on or off.",
    "email_enabled": "Off for an air-gapped install with no relay.",
    "metrics_enabled": "Whether /metrics exists at all.",
    "rate_limit_enabled": "Production refuses to run with this off.",
    "chat_org_admin_reads_private": (
        "Whether an org admin may open a private chat; a deployment's governance answer."
    ),
    "agent_web_fetch_enabled": "Off for an install whose agents may not reach the public web.",
}

#: Settings every production deployment must carry, so every deployment surface
#: must render them: the secrets the production validator refuses to boot
#: without or that decide whether stored data stays readable, and the switches a
#: worker refuses to boot over. A surface that forgets one ships an install that
#: crash-loops on its first start or strands what it already stored, and nothing
#: else in lint notices, because another surface still carries the name.
EVERY_DEPLOYMENT: dict[str, str] = {
    "auth_jwt_secret": "Signs every session; production refuses to boot without it.",
    "token_hash_pepper": "Peppers stored tokens; production refuses to boot without it.",
    "secret_box_key": (
        "Encrypts stored credentials; without it they are keyed off AUTH_JWT_SECRET and "
        "rotating that secret strands every one of them."
    ),
    "database_url": "The application database; the dev default is refused in production.",
    "files_content_signing_key": (
        "The HMAC over every content URL; refused empty or short once Files is on."
    ),
    "compute_require_provider_key": (
        "A worker serving the money queue refuses to boot without a compute provider key "
        "unless this says the install runs no provider-backed plane."
    ),
}


#: What a surface is for. A registry ASSIGNS names (`.env.example`); a docs
#: surface MENTIONS them in prose; a deployment surface RENDERS them onto a
#: running container. An `OPERATOR_TUNED` setting must reach the docs and at
#: least one deployment surface; an `EVERY_DEPLOYMENT` one every deployment
#: surface.
KINDS = ("registry", "docs", "deployment")


@dataclass(frozen=True)
class Surface:
    """One place an operator reads or edits configuration."""

    key: str
    label: str
    #: Files scanned for the environment-variable name, relative to `root`.
    paths: tuple[str, ...]
    #: Directory globs scanned recursively, relative to `root`.
    globs: tuple[str, ...] = ()
    kind: str = "deployment"
    #: The tree the paths are relative to; the repository root when unset.
    root: Path | None = None


#: The registry every install starts from, at the root of each tree.
ENV_EXAMPLE = Surface("env_example", ".env.example", (".env.example",), kind="registry")

#: The file a tree declares its surfaces in, anywhere inside it.
DECLARATION = "settings-surfaces.toml"

#: Directories never searched for a declaration.
_PRUNED = {"node_modules", "vendor", "dist", "build", "__pycache__"}


@dataclass(frozen=True)
class SettingRow:
    """One field of a settings section, with where it is mentioned."""

    field: str
    env_name: str
    group: str
    default: str
    comment: tuple[str, ...]
    present: frozenset[str]
    #: The registry (an example file) the field's section belongs to.
    registry: str = ".env.example"


@dataclass(frozen=True)
class Section:
    """A settings class one tree declares, and the registry that lists it."""

    target: str
    registry: str
    root: Path

    def load(self) -> type[BaseModel]:
        module_name, _, class_name = self.target.partition(":")
        cls = getattr(importlib.import_module(module_name), class_name)
        if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
            raise ValueError(f"settings section {self.target!r} is not a settings class")
        return cls

    @property
    def registry_path(self) -> Path:
        return self.root / self.registry


#: The open platform's settings, the section every tree has even with no
#: declaration (a checkout older than section declarations).
OPEN_SECTION = "alkera_core.config:Settings"


def _section_of_line(headers: Sequence[tuple[int, str]], lineno: int) -> str:
    """Return the `# --- Name ---` banner a line sits under."""
    group = "Ungrouped"
    for header_line, name in headers:
        if header_line > lineno:
            break
        group = name
    return group


def _parse_config_source(source: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Map each `Settings` field to its section banner and its own comment block.

    The comment authored above a field is the only prose that explains what the
    number bounds, so it is what `.env.example` should carry too.
    """
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines()
    headers: list[tuple[int, str]] = []
    field_lines: dict[str, int] = {}
    header_re = re.compile(r"^\s{4}#\s*-{2,}\s*(.+?)\s*-{2,}\s*$")
    comment_re = re.compile(r"^\s{4}#\s?(.*)$")
    field_re = re.compile(r"^\s{4}([a-z][a-z0-9_]*)\s*:")
    for index, line in enumerate(lines):
        header_match = header_re.match(line)
        if header_match is not None:
            headers.append((index, header_match.group(1)))
            continue
        field_match = field_re.match(line)
        if field_match is not None:
            field_lines.setdefault(field_match.group(1), index)

    parsed: dict[str, tuple[str, tuple[str, ...]]] = {}
    for name, lineno in field_lines.items():
        comment: list[str] = []
        cursor = lineno - 1
        while cursor >= 0:
            if header_re.match(lines[cursor]) is not None:
                break
            match = comment_re.match(lines[cursor])
            if match is None:
                break
            comment.append(match.group(1).rstrip())
            cursor -= 1
        parsed[name] = (_section_of_line(headers, lineno), tuple(reversed(comment)))
    return parsed


def _roots() -> list[Path]:
    """This tree, and the checkout it is run from when that one composes it
    (this tree sits inside the working directory)."""
    roots = [REPO_ROOT.resolve()]
    here = Path.cwd().resolve()
    if here != roots[0] and here in roots[0].parents:
        roots.append(here)
    return roots


def _declarations(root: Path, others: Sequence[Path]) -> Iterator[Path]:
    """Every surface declaration in `root`, not descending into another root."""
    skip = {other for other in others if other != root}
    for directory, subdirs, files in os.walk(root):
        current = Path(directory)
        subdirs[:] = sorted(
            d
            for d in subdirs
            if not d.startswith(".") and d not in _PRUNED and current / d not in skip
        )
        if DECLARATION in files:
            yield current / DECLARATION


def parse_declaration(text: str, root: Path) -> tuple[Surface, ...]:
    """The surfaces one declaration names, with paths relative to `root`."""
    surfaces: list[Surface] = []
    for entry in tomllib.loads(text).get("surface", []):
        kind = entry.get("kind", "deployment")
        if kind not in KINDS or kind == "registry":
            raise ValueError(f"surface {entry.get('key')!r}: kind must be docs or deployment")
        surfaces.append(
            Surface(
                key=entry["key"],
                label=entry.get("label", entry["key"]),
                paths=tuple(entry.get("paths", ())),
                globs=tuple(entry.get("globs", ())),
                kind=kind,
                root=root,
            )
        )
    return tuple(surfaces)


def parse_sections(text: str, root: Path) -> tuple[Section, ...]:
    """The settings sections one declaration names, registries relative to `root`."""
    return tuple(
        Section(target=entry["section"], registry=entry.get("registry", ".env.example"), root=root)
        for entry in tomllib.loads(text).get("settings", [])
    )


def load_sections() -> tuple[Section, ...]:
    """Every settings section the trees in this checkout declare."""
    roots = _roots()
    found: dict[str, Section] = {}
    for root in roots:
        for declaration in _declarations(root, roots):
            for section in parse_sections(declaration.read_text(encoding="utf-8"), root):
                if section.target in found:
                    raise ValueError(f"settings section {section.target!r} is declared twice")
                found[section.target] = section
    if not found:
        found[OPEN_SECTION] = Section(OPEN_SECTION, ".env.example", roots[0])
    return tuple(found.values())


def load_surfaces() -> tuple[Surface, ...]:
    """`.env.example`, then every surface the trees in this checkout declare.

    A declaration names its surfaces relative to the root of the tree it sits
    in, so a tree can declare its own deployment files without the others
    naming them."""
    roots = _roots()
    found: dict[str, Surface] = {ENV_EXAMPLE.key: ENV_EXAMPLE}
    for root in roots:
        for declaration in _declarations(root, roots):
            for surface in parse_declaration(declaration.read_text(encoding="utf-8"), root):
                if surface.key in found:
                    raise ValueError(f"surface {surface.key!r} is declared twice")
                found[surface.key] = surface
    return tuple(found.values())


def _iter_surface_files(surface: Surface) -> Iterator[Path]:
    roots = [surface.root] if surface.root is not None else _roots()
    for root in roots:
        for relative in surface.paths:
            candidate = root / relative
            if candidate.is_file():
                yield candidate
        for pattern in surface.globs:
            yield from sorted(path for path in root.glob(pattern) if path.is_file())


def active_surfaces() -> tuple[Surface, ...]:
    """The declared surfaces this checkout holds at least one file of."""
    return tuple(s for s in load_surfaces() if next(_iter_surface_files(s), None) is not None)


def _active_of_kind(kind: str) -> tuple[str, ...]:
    return tuple(s.key for s in active_surfaces() if s.kind == kind)


def active_deployment_surfaces() -> tuple[str, ...]:
    return _active_of_kind("deployment")


def active_docs_surfaces() -> tuple[str, ...]:
    return _active_of_kind("docs")


def _mentions(surface: Surface, env_names: Iterable[str]) -> set[str]:
    """Return the env names mentioned anywhere in a surface's files."""
    wanted = set(env_names)
    found: set[str] = set()
    for path in _iter_surface_files(surface):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for token in re.findall(r"[A-Z][A-Z0-9_]{2,}", text):
            if token in wanted:
                found.add(token)
    return found


#: Helm's own comments, which are not `#` lines: `{{/* ... */}}`, optionally
#: whitespace-trimmed. Stripped before a file is read for renders.
_TEMPLATE_COMMENT = re.compile(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", re.DOTALL)

#: A line that gives a setting a VALUE, in each shape these surfaces use:
#: `NAME: value` (YAML mapping, Helm template), `NAME = value` (HCL),
#: `- NAME=value` (a compose env list) and `- name: NAME` (a Kubernetes
#: container env entry, whose value is on the following line).
_RENDERS = (
    re.compile(r"""^\s*-?\s*["']?([A-Z][A-Z0-9_]{2,})["']?\s*[:=]"""),
    re.compile(r"""^\s*-\s*name:\s*["']?([A-Z][A-Z0-9_]{2,})["']?\s*$"""),
)


def _renders(surface: Surface, env_names: Iterable[str]) -> set[str]:
    """Return the env names a surface actually RENDERS, not merely names.

    A deployment surface earns its entry in the matrix by putting the setting on
    a container, not by discussing it: a name that survives only in a comment
    reaches no running process, which is the exact gap this checker exists to
    close (terraform used to "carry" `GATEWAY_MAX_STREAM_SECONDS` that way).
    Prose still counts on `.env.example` and the deploy notes, which are read
    rather than applied, and `.env.example` has its own stricter rule.
    """
    wanted = set(env_names)
    found: set[str] = set()
    for path in _iter_surface_files(surface):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in _TEMPLATE_COMMENT.sub("", text).splitlines():
            if line.lstrip().startswith("#"):
                continue
            for pattern in _RENDERS:
                match = pattern.match(line)
                if match is not None and match.group(1) in wanted:
                    found.add(match.group(1))
    return found


def registry_assignments(path: Path) -> set[str]:
    """Return the names one registry actually ASSIGNS, not merely mentions.

    A name that appears only inside a comment is not a registry entry; an
    operator copying the file gets nothing from it. A commented-out
    ``# NAME=default`` line documents the default and counts.
    """
    if not path.is_file():
        return set()
    assigned: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", line)
        if match is not None:
            assigned.add(match.group(1))
    return assigned


def _format_default(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is Ellipsis:
        return "<required>"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def collect_rows(sections: Sequence[Section] | None = None) -> list[SettingRow]:
    """Build the settings x surfaces matrix over every declared section."""
    declared = tuple(load_sections() if sections is None else sections)
    owned: list[tuple[Section, str, FieldInfo, Path]] = []
    for section in declared:
        cls = section.load()
        source = Path(inspect.getsourcefile(cls) or CONFIG_SOURCE)
        for name, field in cls.model_fields.items():
            owned.append((section, name, field, source))
    env_names = {name.upper() for _, name, _, _ in owned}

    present_by_surface: dict[str, set[str]] = {}
    for surface in load_surfaces():
        if surface.kind == "registry":
            continue
        if surface.kind == "deployment":
            present_by_surface[surface.key] = _renders(surface, env_names)
        else:
            present_by_surface[surface.key] = _mentions(surface, env_names)

    groups_by_source: dict[Path, dict[str, tuple[str, tuple[str, ...]]]] = {}
    assigned_by_registry: dict[Path, set[str]] = {}
    rows: list[SettingRow] = []
    for section, name, field, source in owned:
        env_name = name.upper()
        registry = section.registry_path
        if registry not in assigned_by_registry:
            assigned_by_registry[registry] = registry_assignments(registry)
        present = {key for key, names in present_by_surface.items() if env_name in names}
        if env_name in assigned_by_registry[registry]:
            present.add(ENV_EXAMPLE.key)
        if source not in groups_by_source:
            groups_by_source[source] = _parse_config_source(source)
        group, comment = groups_by_source[source].get(name, ("Ungrouped", ()))
        rows.append(
            SettingRow(
                field=name,
                env_name=env_name,
                group=group,
                default=_format_default(field.default),
                comment=comment,
                present=frozenset(present),
                registry=section.registry,
            )
        )
    return rows


def ownership_problems(sections: Sequence[Section] | None = None) -> list[str]:
    """One owner per setting, and no registry listing a setting another owns."""
    declared = tuple(load_sections() if sections is None else sections)
    owner: dict[str, Section] = {}
    problems: list[str] = []
    for section in declared:
        for name in section.load().model_fields:
            if name in owner:
                problems.append(
                    f"`{name.upper()}` is declared by both {owner[name].target} and "
                    f"{section.target}; a setting has one owner."
                )
                continue
            owner[name] = section
    registries = {section.registry_path: section for section in declared}
    for path, section in registries.items():
        for env_name in sorted(registry_assignments(path)):
            other = owner.get(env_name.lower())
            if other is not None and other.registry_path != path:
                problems.append(
                    f"{section.registry} lists `{env_name}`, which {other.target} declares "
                    f"for {other.registry}; list it only there."
                )
    return problems


def emit_missing(rows: Sequence[SettingRow]) -> str:
    """Render `.env.example` blocks for every setting the registry is missing.

    Grouped by the section banner in `config.py`, each entry carrying the field's
    own comment on its own lines: the shape `.env.example` is required to hold.
    """
    missing = [
        row for row in rows if "env_example" not in row.present and row.field not in DEV_ONLY
    ]
    chunks: list[str] = []
    current_group = ""
    for row in missing:
        if row.group != current_group:
            current_group = row.group
            chunks.append(f"\n# --- {current_group} ---")
        for line in row.comment:
            chunks.append(f"# {line}".rstrip())
        # Commented out on purpose: the line documents the shipped default without
        # assigning it. `make bootstrap` copies this file to `.env`, and eagerly
        # assigning an optional setting turns "unset" into the empty string, which
        # several of them treat as a real value.
        chunks.append(f"# {row.env_name}={row.default}")
    return "\n".join(chunks)


def check(
    rows: Sequence[SettingRow],
    deployment_surfaces: Sequence[str] | None = None,
    docs_surfaces: Sequence[str] | None = None,
) -> list[str]:
    """Return one message per violation; empty means the surfaces are honest.

    ``deployment_surfaces`` and ``docs_surfaces`` are the surfaces held to the
    rules, by default the ones this checkout declares and holds."""
    deployment = tuple(
        active_deployment_surfaces() if deployment_surfaces is None else deployment_surfaces
    )
    docs = tuple(active_docs_surfaces() if docs_surfaces is None else docs_surfaces)
    known = {row.field for row in rows}
    by_field = {row.field: row for row in rows}
    problems: list[str] = []
    if not deployment:
        problems.append(
            f"No deployment surface is in this checkout (declare one in a {DECLARATION}), "
            "so no rule below can hold."
        )

    for field, reason in sorted(DEV_ONLY.items()):
        if field not in known:
            problems.append(
                f"DEV_ONLY names `{field}`, which is no longer a Settings field; drop the entry."
            )
            continue
        if "env_example" in by_field[field].present:
            problems.append(
                f"`{by_field[field].env_name}` is in .env.example but DEV_ONLY says it should "
                f"not be ({reason}); remove one of the two."
            )

    for field, reason in sorted(OPERATOR_TUNED.items()):
        if field not in known:
            problems.append(
                f"OPERATOR_TUNED names `{field}`, which is no longer a Settings field; "
                f"drop the entry."
            )
            continue
        row = by_field[field]
        if docs and not row.present.intersection(docs):
            problems.append(
                f"`{row.env_name}` is operator-tuned ({reason}) but is not in the "
                f"operator docs ({', '.join(docs)})."
            )
        if deployment and not row.present.intersection(deployment):
            problems.append(
                f"`{row.env_name}` is operator-tuned ({reason}) but no deployment surface "
                f"renders it (expected one of {', '.join(deployment)})."
            )

    for field, reason in sorted(EVERY_DEPLOYMENT.items()):
        if field not in known:
            problems.append(
                f"EVERY_DEPLOYMENT names `{field}`, which is no longer a Settings field; "
                f"drop the entry."
            )
            continue
        row = by_field[field]
        absent = [key for key in deployment if key not in row.present]
        if absent:
            problems.append(
                f"`{row.env_name}` must reach every deployment ({reason}) but "
                f"{', '.join(absent)} does not render it."
            )

    missing = [
        row for row in rows if "env_example" not in row.present and row.field not in DEV_ONLY
    ]
    if missing:
        listed = "\n".join(
            f"    {row.env_name}={row.default}    # {row.group}, in {row.registry}"
            for row in missing
        )
        problems.append(
            f"{len(missing)} setting(s) reach no operator surface at all. Add them to "
            f"their registry (the example file their section is declared with), or name "
            f"them in DEV_ONLY with a reason:\n{listed}"
        )
    return problems


def report(rows: Sequence[SettingRow]) -> str:
    """Render the matrix as a Markdown table."""
    shown = active_surfaces()
    header = "| setting | group | default | " + " | ".join(s.label for s in shown) + " |"
    divider = "| --- | --- | --- | " + " | ".join("---" for _ in shown) + " |"
    lines = [header, divider]
    for row in sorted(rows, key=lambda item: (item.group, item.field)):
        marks = " | ".join("yes" if s.key in row.present else "-" for s in shown)
        default = f"`{row.default}`" if row.default else ""
        lines.append(f"| `{row.env_name}` | {row.group} | {default} | {marks} |")
    counts = {s.label: sum(1 for row in rows if s.key in row.present) for s in shown}
    summary = ", ".join(f"{label} {count}/{len(rows)}" for label, count in counts.items())
    lines.append("")
    lines.append(f"Coverage: {summary}.")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        action="store_true",
        help="print the settings x surfaces matrix instead of enforcing it",
    )
    parser.add_argument(
        "--emit-missing",
        action="store_true",
        help="print paste-ready .env.example blocks for the settings it is missing",
    )
    args = parser.parse_args(argv)

    rows = collect_rows()
    if args.report:
        print(report(rows))
        return 0
    if args.emit_missing:
        print(emit_missing(rows))
        return 0

    problems = ownership_problems() + check(rows)
    if problems:
        print("Settings are not reachable from the surfaces an operator edits:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nRun `python scripts/check_settings_surfaces.py --report` for the full matrix.",
            file=sys.stderr,
        )
        return 1
    print(f"settings surfaces: {len(rows)} settings, every one reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
