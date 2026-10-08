"""Where a chat's folder is in the drive, and where its work lands when the
folder is gone.

The chat record names the node its folder IS and the drive that node is on;
a record that names neither falls back to ``Chats/<chat id>``. When the folder
has been trashed or purged while a box held it, the work is landed under the
owner's home in a recovery folder stamped with the day, so a person scanning
the drive can tell what it is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from alkera_core.files.providers.registry import POINTER_EXTENSIONS
from alkera_sdk.client import AlkeraHTTPError

from alkera_cli.files.push import FilesApi

#: Where chat folders live in the org drive when the chat record does not say.
CHAT_FOLDER_ROOT = "Chats"

#: Where the work of a chat whose folder is gone is landed instead, under the
#: owner's home. A place a person can find beats a drop.
RECOVERY_ROOT = f"{CHAT_FOLDER_ROOT}/Recovered"

#: What a chat folder's name ends in. Read off the one registry the server
#: names object nodes with, because the drive reads a node's type back off the
#: same suffix — a folder that has lost it is no longer a chat anybody can open.
CHAT_FOLDER_SUFFIX = POINTER_EXTENSIONS["chat"]

#: How many names one recovered stem is tried under before the work is landed
#: in the last of them. Only a collision costs a round trip, so this is a bound
#: on a pathological day rather than a number anyone reaches.
RECOVERY_NAME_ATTEMPTS = 50

#: What a chat record may call its own folder. Read duck-typed rather than off a
#: typed field because the Files side of the chat-as-a-folder shape lands
#: separately; a record that carries neither falls back to the convention.
FOLDER_PATH_KEYS: tuple[str, ...] = ("folder_path", "folderPath")

#: Where a chat record names the Files node it IS. This is the identity; a path
#: is only a name, and two of them can be filed under one name over time.
FOLDER_NODE_KEYS: tuple[str, ...] = ("files_node_id", "filesNodeId")

#: Where a chat record names the drive its folder is on. The drive is a fact
#: about the chat, not about whoever is asking: a pool box serves chats from
#: many orgs, and a box on its machine credential has no drive of its own to
#: stand in.
FOLDER_DRIVE_KEYS: tuple[str, ...] = ("files_drive_id", "filesDriveId")


def _named(chat: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        raw = chat.get(key)
        cleaned = raw.strip() if isinstance(raw, str) else ""
        if cleaned:
            return cleaned
    return None


def chat_folder_node(chat: Mapping[str, Any]) -> str | None:
    """The id of the Files node ``chat`` is, when the record names one.

    ``None`` for a chat the drive does not hold — Files is off, or the chat
    predates it — which is what puts the caller back on the path convention.
    """
    return _named(chat, FOLDER_NODE_KEYS)


def chat_folder_drive(chat: Mapping[str, Any]) -> str | None:
    """The id of the drive ``chat``'s folder is on, when the record names one.

    ``None`` puts the caller back on its own drive: right for an org box on
    its operator's session, and nothing at all for a box on its machine
    credential, which the server answers has no drive.
    """
    return _named(chat, FOLDER_DRIVE_KEYS)


def chat_folder_path(chat: Mapping[str, Any], chat_id: str) -> str:
    """Where ``chat``'s folder lives in the org drive, without a leading slash."""
    for key in FOLDER_PATH_KEYS:
        raw = chat.get(key)
        cleaned = raw.strip().strip("/").strip() if isinstance(raw, str) else ""
        if cleaned:
            return cleaned
    return f"{CHAT_FOLDER_ROOT}/{chat_id}"


def wire_path(item: Mapping[str, Any]) -> str | None:
    """The drive path an item payload carries, without its leading slash.

    The wire serves ``pathBytes`` (the path as the names really are) and no
    display ``path``. The push and the release address a folder by path, so the
    path they are given has to be where the node really is: the convention
    (``Chats/<chat id>``) is not where a chat's ``.alkerachat`` folder lives — it
    sits in its owner's home under the chat's title — and a push aimed at the
    convention would raise a stray folder at the drive root and file the chat's
    work there.
    """
    raw = item.get("pathBytes") or item.get("path_bytes") or item.get("path")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "surrogateescape")
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().strip("/")
    return cleaned or None


def recovery_stem(name: str) -> str:
    """``name`` with its chat extension and any path in it taken off.

    A folder named by the convention carries no extension at all, and one named
    by its title carries exactly one — which must not become two.
    """
    cleaned = name.strip().strip("/").replace("/", "-").strip()
    if cleaned.endswith(CHAT_FOLDER_SUFFIX):
        cleaned = cleaned[: -len(CHAT_FOLDER_SUFFIX)].strip()
    return cleaned or "chat"


#: What a member's own home is called where a person reads a path: its stored
#: name is the member's id, which means nothing to them.
HOME_LABEL = "Home"


def shown_in_home(org_path: str, home_path: str) -> str:
    """``org_path`` as its owner reads it: under ``Home`` rather than their id.

    A path outside the home is returned as it is.
    """
    home = home_path.strip("/")
    path = org_path.strip("/")
    if home and (path == home or path.startswith(home + "/")):
        return f"{HOME_LABEL}{path[len(home) :]}"
    return org_path


def recovery_path(
    home_path: str,
    name: str,
    *,
    when: datetime | None = None,
    taken: Callable[[str], bool] | None = None,
) -> str:
    """Where the work of a chat whose folder is gone is landed instead.

    Under the owner's own home rather than beside the chats, and stamped with
    the day, so a person scanning their drive can tell what they are looking at.

    The stamp goes INSIDE the name, in front of the extension. A chat is a chat
    to the drive because its folder ends in ``.alkerachat``: a name stamped
    after the extension lands the work as a folder nobody can open as a chat,
    which is the one thing a recovery exists to prevent.

    ``taken`` — a drive lookup, when the caller has one — walks a counter
    through the same stem until the name is free, so a second recovery on the
    same day is its own folder rather than a landing on top of the first.
    """
    root = f"{home_path.strip('/')}/{RECOVERY_ROOT}".strip("/")
    stamp = (when or datetime.now(UTC)).strftime("%Y-%m-%d")
    stem = recovery_stem(name)
    candidate = f"{root}/{stem} (recovered {stamp}){CHAT_FOLDER_SUFFIX}"
    if taken is None:
        return candidate
    for counter in range(2, RECOVERY_NAME_ATTEMPTS + 1):
        if not taken(candidate):
            return candidate
        candidate = f"{root}/{stem} (recovered {stamp}) {counter}{CHAT_FOLDER_SUFFIX}"
    # A stem this crowded is not worth another round trip per pass: the work
    # lands in the last folder rather than not landing at all.
    return candidate


def home_path(files: FilesApi) -> str:
    """The drive path of the caller's own home, or the drive root.

    The recovery folder belongs to a person, not to the org's landing
    screen, so it is anchored on the home the drive names. A drive that
    names none puts it at the root, which is still somewhere findable; so
    does a caller that has no drive of its own to ask about — a box on its
    machine credential — whose recovery is then refused at the root like
    any other write outside its chats, and said so, rather than lost to an
    error on the way.
    """
    try:
        drive = files.drive()
    except (httpx.HTTPError, AlkeraHTTPError, OSError):
        return ""
    home_id = drive.get("homeId") or drive.get("home_id")
    if not isinstance(home_id, str) or not home_id.strip():
        return ""
    try:
        return wire_path(files.item(str(files.drive()["id"]), home_id.strip())) or ""
    except (httpx.HTTPError, AlkeraHTTPError, OSError):
        return ""


def occupied(files: FilesApi, path: str) -> bool:
    """Whether the drive already holds a node at ``path``.

    Only a clean answer counts as taken. A refusal or a dead connection
    reads as free, because landing beside — or even inside — an earlier
    recovery beats walking the counter over a drive that is not answering.
    """
    try:
        files.item_by_path(str(files.drive()["id"]), path)
    except (AlkeraHTTPError, httpx.HTTPError, OSError):
        return False
    return True


__all__ = [
    "CHAT_FOLDER_ROOT",
    "CHAT_FOLDER_SUFFIX",
    "FOLDER_DRIVE_KEYS",
    "FOLDER_NODE_KEYS",
    "FOLDER_PATH_KEYS",
    "HOME_LABEL",
    "RECOVERY_NAME_ATTEMPTS",
    "RECOVERY_ROOT",
    "chat_folder_drive",
    "chat_folder_node",
    "chat_folder_path",
    "home_path",
    "occupied",
    "recovery_path",
    "recovery_stem",
    "shown_in_home",
    "wire_path",
]
