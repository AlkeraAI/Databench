"""Files inside a chat's working directory, as a message names them.

A message points at a file in the chat one way — a path relative to the
agent's working directory (the sandbox), ``![Image 1](uploads/paste-1-ab12.png)``
for an image, ``[File 1: q.csv](uploads/file-1-cd34.csv)`` for anything else. These are
the pure rules behind the daemon methods that write a pasted file into that
directory and map a path back to a file the editor can show: the stored name
of an upload, and the one way a relative path resolves (inside the sandbox or
not at all). No I/O beyond a final existence check, so a test drives every
branch with ``tmp_path``.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path, PurePosixPath
from typing import Literal

from alkera_cli.host.limits import env_count

UploadKind = Literal["image", "file"]

#: The size cap on one file staged into a chat's working directory, both kinds.
#:
#: This is a bound on the MESSAGE, not on what Alkera Files accepts. A staged
#: file crosses the daemon as one base64 JSON-RPC request held whole in memory
#: on both sides — it never touches the Files session API, the edge or the
#: object store — so it is deliberately far below the deployment's file ceiling
#: (``files_max_file_bytes``, 1000 GB) rather than following it. A file too
#: large to paste into a chat is one to put in Files and reference.
#:
#: The webview states the same number so a reader is refused before the bytes
#: are read off disk (``STAGE_FILE_MAX_BYTES`` in
#: the editor's daemon data source); this one is what actually
#: refuses, whatever the client believes.
#:
#: A deployment whose daemon and editor sit on the same machine can afford a
#: bigger one; ``ALKERA_STAGE_FILE_MAX_BYTES`` (bytes) moves it. A non-positive
#: value keeps the default — this bound cannot be removed, because the whole
#: file is held in memory on both sides of the request.
ENV_STAGE_FILE_MAX_BYTES = "ALKERA_STAGE_FILE_MAX_BYTES"
_STAGE_FILE_MAX_BYTES_DEFAULT = 10 * 1024 * 1024
STAGE_FILE_MAX_BYTES = (
    env_count(os.environ.get(ENV_STAGE_FILE_MAX_BYTES), default=_STAGE_FILE_MAX_BYTES_DEFAULT)
    or _STAGE_FILE_MAX_BYTES_DEFAULT
)

_EXT = re.compile(r"[^a-z0-9]")

#: The folder, under the chat's working directory, every file a person hands
#: the chat is put in: apart from what the agent writes, and named in the
#: agent's brief so it knows where to look. The browser shell uses the same
#: name (``UPLOADS_FOLDER`` in ``apps/web/src/pages/workspace/chat/data/chatFiles.ts``).
UPLOADS_FOLDER = "uploads"

#: How many fresh names :func:`write_staged_file` draws before giving up on a
#: folder so full that every one it drew is taken.
_STAGE_NAME_ATTEMPTS = 64


def stage_file_size_label() -> str:
    """The cap as the refusal says it — decimal units, the way the API states
    its own ceiling, so one product does not quote two kinds of megabyte."""
    return f"{STAGE_FILE_MAX_BYTES // 1_000_000} MB"


def staged_file_name(kind: UploadKind, n: int, original: str) -> str:
    """The path a pasted or attached file is stored at, relative to the chat root.

    Always under :data:`UPLOADS_FOLDER`. Deterministic in shape —
    ``uploads/paste-<n>-<id>.<ext>`` for an image, ``uploads/file-<n>-<id>.<ext>``
    for anything else. The id is short and random, so two calls can answer the
    same name; :func:`write_staged_file` is what guarantees an earlier upload is
    never overwritten. The extension is the original's, lower-cased and stripped
    to what a path may carry.
    """
    stem = "paste" if kind == "image" else "file"
    suffix = secrets.token_hex(2)
    base = PurePosixPath(original.replace("\\", "/")).name
    ext = _EXT.sub("", base.rsplit(".", 1)[1].lower()) if "." in base[1:] else ""
    name = f"{stem}-{n}-{suffix}.{ext}" if ext else f"{stem}-{n}-{suffix}"
    return f"{UPLOADS_FOLDER}/{name}"


def write_staged_file(root: Path, kind: UploadKind, n: int, original: str, data: bytes) -> str:
    """Write ``data`` into ``root`` at a fresh :func:`staged_file_name` and
    answer that chat-relative path. The uploads folder is made on the way.

    The file is created exclusively: a name an earlier upload already holds is
    drawn again, never written over, so the image an older message shows keeps
    showing what it showed. Two pastes numbered alike (every message numbers
    from 1) meet the four-hex id's birthday bound after a few hundred uploads.
    """
    for _ in range(_STAGE_NAME_ATTEMPTS):
        rel = staged_file_name(kind, n, original)
        target = root / Path(*PurePosixPath(rel).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as fh:
                fh.write(data)
        except FileExistsError:
            continue
        return rel
    raise FileExistsError(f"no free upload name for {kind} {n} after {_STAGE_NAME_ATTEMPTS} tries")


def relative_chat_path(target: str) -> PurePosixPath | None:
    """``target`` as a path inside the chat root, or ``None`` when it is not one.

    Refuses everything the renderer refuses: an absolute path, a URL, a
    Windows separator, an empty segment, ``.`` or ``..`` anywhere.
    """
    if not target or target != target.strip():
        return None
    if "\\" in target or target.startswith("/") or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
        return None
    parts = target.removeprefix("./").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return PurePosixPath(*parts)


def resolve_chat_file(root: Path, target: str) -> Path | None:
    """The file ``target`` names inside ``root``, or ``None``.

    ``None`` for a path that leaves the root (also through a symlink, since
    the resolved path is what is checked) and for one that names nothing.
    """
    rel = relative_chat_path(target)
    if rel is None:
        return None
    base = root.resolve()
    candidate = (base / Path(*rel.parts)).resolve()
    if candidate != base and base not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


__all__ = [
    "STAGE_FILE_MAX_BYTES",
    "UPLOADS_FOLDER",
    "UploadKind",
    "relative_chat_path",
    "resolve_chat_file",
    "staged_file_name",
    "write_staged_file",
]
