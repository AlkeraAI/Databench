"""Reading and writing the spec and ``ENVIRONMENT.md`` in a workspace root.

A cloud box's daemon runs as root and a chat's folder is the chat's to change,
so a read here never follows a symlink and never reads anything but a regular
file of bounded size with one name: a chat that pointed ``ENVIRONMENT.md``
at a host file (a symlink, or a hard link made where the chat and a host file
share a filesystem) would otherwise have the box read it into its own
prompt. Writes in a chat's
folder do not happen here at all; the environment tool writes them inside the
chat's sandbox, as the chat's user.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from alkera_core.project import write_text_atomic
from pydantic import ValidationError

from alkera_cli.environment.redact import SpecSecretError, find_secrets
from alkera_cli.environment.spec import SPEC_FILENAME, EnvironmentSpec
from alkera_cli.files.chat_fs import ChatTree

#: The largest spec a reader accepts.
MAX_SPEC_BYTES = 8 * 1024 * 1024


def read_workspace_text(root: Path, name: str, *, max_bytes: int) -> str | None:
    """The text of the regular file ``name`` directly in ``root``, or ``None``
    when it is missing, a link (symbolic, or a regular file with a second
    name, which is how a hard link to a file elsewhere looks), a FIFO or
    anything but a regular file, or larger than ``max_bytes``. Read through
    the chat tree's one read seam
    (:class:`~alkera_cli.files.chat_fs.ChatTree`): no link followed and the
    open never blocks, so a FIFO planted at the name cannot hang a chat's
    start."""
    tree = ChatTree(root)
    try:
        info = tree.stat(name)
        if (
            info is None
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink > 1
            or info.st_size > max_bytes
        ):
            return None
        with tree.open_read(name) as handle:
            # Read again off the open file: a name swapped for a hard link
            # between the check above and the open is refused too.
            if os.fstat(handle.fileno()).st_nlink > 1:
                return None
            data = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(data) > max_bytes:
        return None
    return data.decode("utf-8", "replace")


def render_spec(spec: EnvironmentSpec) -> str:
    """The spec as the file's text; :class:`SpecSecretError` when anything in
    it is credential-shaped."""
    data = spec.model_dump(mode="json")
    hits = find_secrets(data)
    if hits:
        raise SpecSecretError(hits)
    return spec.model_dump_json(indent=2) + "\n"


class InvalidSpecError(ValueError):
    """The workspace's spec is missing, unreadable, or holds a value no
    recreate may act on; nothing in it is used."""


def load_spec_text(text: str) -> EnvironmentSpec:
    """The spec in ``text``, every value validated (see :mod:`.spec`)."""
    try:
        return EnvironmentSpec.model_validate_json(text)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise InvalidSpecError(
            f"{SPEC_FILENAME} is not a valid environment spec ({where}: {first.get('msg', '')})"
        ) from exc
    except ValueError as exc:
        raise InvalidSpecError(f"{SPEC_FILENAME} is not valid JSON") from exc


def parse_spec(text: str) -> EnvironmentSpec | None:
    try:
        return load_spec_text(text)
    except InvalidSpecError:
        return None


def load_spec(root: Path) -> EnvironmentSpec:
    """The workspace's spec, or :class:`InvalidSpecError` saying why there is
    none to use."""
    text = read_workspace_text(root, SPEC_FILENAME, max_bytes=MAX_SPEC_BYTES)
    if text is None:
        raise InvalidSpecError(f"no readable {SPEC_FILENAME} in the workspace")
    return load_spec_text(text)


def read_spec(root: Path) -> EnvironmentSpec | None:
    try:
        return load_spec(root)
    except InvalidSpecError:
        return None


def write_spec(root: Path, spec: EnvironmentSpec) -> Path:
    """Write the spec into ``root`` on this machine, atomically. For a workspace
    this user owns; a chat's folder is written from inside its sandbox."""
    target = root / SPEC_FILENAME
    # A file of the person's project, which they may commit: the umask's mode.
    write_text_atomic(target, render_spec(spec), mode=None)
    return target


__all__ = [
    "MAX_SPEC_BYTES",
    "InvalidSpecError",
    "load_spec",
    "load_spec_text",
    "parse_spec",
    "read_spec",
    "read_workspace_text",
    "render_spec",
    "write_spec",
]
