"""What an agent's markdown points at inside its own chat folder. Pure strings.

The agent writes references relative to the directory it runs in -- the chat's
working folder -- because that is what it sees: ``![chart](charts/revenue.png)``,
``[the data](./out.csv)``, `` `report.md` ``, and sometimes the absolute path on
its box, ``/opt/alkera-work/.alkera/chats/<chat>/scratch/charts/revenue.png``.
Two sides need the same answer about such a reference:

* the box, which publishes a reply only once the files it names have their
  bytes on the drive (so a reader shown the reference can open it);
* every surface that shows the reply outside the web transcript (a Slack
  thread), which turns the reference into the file it names in Files.

So the rules live here, once, with no filesystem and no database:
:func:`chat_references` finds the targets in a message and :func:`chat_path`
says which of them name a file inside the chat. They mirror the web
transcript's (``chatPaths.ts``): a reference is a path INSIDE the chat's folder
or it is nothing -- a URL of any scheme, a ``..`` step, a backslash, a query or
fragment, an absolute path that is not this chat's folder on a box all answer
``None``, so a reference can never name another chat's file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote

#: The image types a chat renders inline -- the web's ``CHAT_IMAGE_EXTENSIONS``,
#: word for word, so a reference the web shows as a picture is one every other
#: surface treats as a picture too.
CHAT_IMAGE_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg"})

#: Where a chat's folder sits on a box: ``<anything>/.alkera/chats/<chat id>/``.
_BOX_CHAT_ROOT = re.compile(r"/\.alkera/chats/(?P<chat>[^/]+)/(?P<rest>.+)$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(([^)\s]+)\)")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
#: Inline code that looks like a path (``report.csv``, ``charts/x.png``) rather
#: than code (``SELECT 1``, ``x``).
_PATHLIKE = re.compile(r"^[^\s`]*[./][^\s`]*$")

Anchor = Literal["working", "chat"]


@dataclass(frozen=True, slots=True)
class ChatReference:
    """A link or image the agent's markdown points at, as written.

    ``target`` is the raw target (``charts/x.png``, ``./plot.py``, a box path,
    a URL); ``label`` the link text or the image's alt; ``image`` whether it was
    written as an image (``![..](..)``).
    """

    target: str
    label: str
    image: bool


def chat_references(text: str) -> list[ChatReference]:
    """Every link and image target in ``text`` outside code, in order, once.

    Path-like inline code counts as a reference too: an agent that says "saved
    to `report.csv`" names that file as surely as a link would. Which of the
    targets are files in the chat is :func:`chat_path`'s call.
    """
    seen: set[str] = set()
    out: list[ChatReference] = []
    prose = _FENCE.sub("", text)
    for match in _INLINE_CODE.finditer(prose):
        target = match.group(1)
        if _PATHLIKE.match(target) and target not in seen:
            seen.add(target)
            out.append(ChatReference(target, target, image=False))
    prose = _INLINE_CODE.sub("", prose)
    for match in _IMAGE.finditer(prose):
        target = match.group(2)
        if target not in seen:
            seen.add(target)
            alt = match.group(1).strip()
            out.append(ChatReference(target, alt or target.rsplit("/", 1)[-1], image=True))
    for match in _LINK.finditer(_IMAGE.sub("", prose)):
        target = match.group(2)
        if target not in seen:
            seen.add(target)
            out.append(ChatReference(target, match.group(1), image=False))
    return out


@dataclass(frozen=True, slots=True)
class ChatPath:
    """A reference, normalized: the steps down from ``anchor``.

    ``working`` is the chat's working folder (what a relative path the agent
    wrote is relative to); ``chat`` is the chat's own folder (what a box's
    absolute path is rooted at).
    """

    path: str
    anchor: Anchor

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def is_image(self) -> bool:
        name = self.name
        dot = name.rfind(".")
        return dot > 0 and name[dot + 1 :].lower() in CHAT_IMAGE_EXTENSIONS


def _steps(raw: str) -> str | None:
    """``raw`` as clean posix steps, or ``None`` when any step climbs or is empty."""
    if "\\" in raw or re.search(r"[?#]", raw):
        return None
    segments = raw.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return None
    return "/".join(segments)


def chat_path(target: str, *, chat_id: str) -> ChatPath | None:
    """What ``target`` names inside chat ``chat_id``'s folder, or ``None``.

    Percent-escapes are decoded (a model that wrote ``my%20chart.png`` means
    the file with the space), a leading ``./`` is dropped, and a box's absolute
    path into THIS chat's folder is rebased onto the chat folder. Everything
    else absolute -- another chat's folder, a system path, a URL -- is not a
    chat file.
    """
    if not target or target != target.strip():
        return None
    if _SCHEME.match(target):
        return None
    decoded = unquote(target)
    if decoded.startswith("/"):
        match = _BOX_CHAT_ROOT.search(decoded)
        if match is None or match.group("chat") != chat_id:
            return None
        steps = _steps(match.group("rest"))
        return None if steps is None else ChatPath(steps, "chat")
    while decoded.startswith("./"):
        decoded = decoded[2:]
    steps = _steps(decoded)
    return None if steps is None else ChatPath(steps, "working")


#: A line that is SOLELY a markdown image -- the web renderer's ``IMAGE_LINE``
#: (``markdownBlocks.ts``). Only such a line is shown as a picture; an image
#: written mid-sentence is shown as its words.
_IMAGE_LINE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)$")
_FENCE_OPEN = re.compile(r"^```([A-Za-z0-9_-]+)?\s*$")
_FENCE_CLOSE = re.compile(r"^```\s*$")
_MATH_FENCE = re.compile(r"^\$\$\s*$")
_QUOTE_LINE = re.compile(r"^>\s?")


@dataclass(frozen=True, slots=True)
class ChatImage:
    """A picture a message shows: its alt text (the file's name when the
    writer gave none), the target as written, and the file it names."""

    alt: str
    target: str
    path: ChatPath


def chat_images(text: str, *, chat_id: str) -> list[ChatImage]:
    """Every picture ``text`` shows from chat ``chat_id``'s folder, in order.

    The web transcript's rule, which every other surface follows so a reader
    of the thread gets the same pictures a reader of the chat does: a line
    that is solely ``![alt](target)``, outside fenced code, math and quotes,
    whose target names a file of an image type in this chat. The cases both
    sides are pinned against live in
    ``packages/api-core/tests/fixtures/chat_paths/images.json``.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[ChatImage] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        closing = _FENCE_CLOSE if _FENCE_OPEN.match(line) else _MATH_FENCE
        if _FENCE_OPEN.match(line) or _MATH_FENCE.match(line):
            index += 1
            while index < len(lines) and not closing.match(lines[index]):
                index += 1
            index += 1
            continue
        index += 1
        if _QUOTE_LINE.match(line):
            continue
        match = _IMAGE_LINE.match(line.strip())
        if match is None:
            continue
        found = chat_path(match.group(2), chat_id=chat_id)
        if found is None or not found.is_image:
            continue
        out.append(ChatImage(match.group(1).strip() or found.name, match.group(2), found))
    return out


#: The web renderer's inline tokenizer (``INLINE_PATTERN`` in
#: ``markdownInline.tsx``), alternative for alternative: a link inside a code
#: span or a bold run is not a link there, so it is not one here either.
_INLINE = re.compile(
    r"\[([^\]]+)\]\(([^)]+)\)"
    r"|\*\*((?:`[^`]+`|[^*`]|`)+)\*\*"
    r"|`([^`]+)`"
    r"|\$([^$\s](?:[^$\n]*[^$\s])?)\$(?!\d)"
    r"|\*((?:`[^`\n]+`|[^*`\n]|`)+)\*"
    r"|_((?:`[^`\n]+`|[^_`\n]|`)+)_"
    r"|(\\\$)"
)
_WEB = re.compile(r"^https?://", re.IGNORECASE)
_BLOB = re.compile(r"^blob:(?://)?")


@dataclass(frozen=True, slots=True)
class ChatLink:
    """A file a message links to in its own chat: the label, the target as
    written, and the file it names."""

    label: str
    target: str
    path: ChatPath


def chat_file_links(text: str, *, chat_id: str) -> list[ChatLink]:
    """Every file ``text`` LINKS to in chat ``chat_id``'s folder, in order, once.

    The web transcript's rule for a link it opens as a chat file
    (``ChatFileLink``): a ``[label](target)`` its inline tokenizer reads as a
    link -- not inside a code span or a bold run, not written as an image,
    not a ``blob:`` handle or a web URL -- whose target names a path in this
    chat, outside fenced code and math. A picture on a line of its own is
    :func:`chat_images`'s, not a link. Together the two are what a surface
    other than the web (a Slack thread) attaches; the cases both sides are
    pinned against live in
    ``packages/api-core/tests/fixtures/chat_paths/links.json``.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[ChatLink] = []
    seen: set[str] = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        closing = _FENCE_CLOSE if _FENCE_OPEN.match(line) else _MATH_FENCE
        if _FENCE_OPEN.match(line) or _MATH_FENCE.match(line):
            index += 1
            while index < len(lines) and not closing.match(lines[index]):
                index += 1
            index += 1
            continue
        index += 1
        if _IMAGE_LINE.match(line.strip()):
            continue
        for match in _INLINE.finditer(line):
            label, target = match.group(1), match.group(2)
            if label is None or target is None:
                continue
            if match.start() > 0 and line[match.start() - 1] == "!":
                continue
            if _BLOB.match(target) or _WEB.match(target):
                continue
            found = chat_path(target, chat_id=chat_id)
            if found is None or found.path in seen:
                continue
            seen.add(found.path)
            out.append(ChatLink(label, target, found))
    return out


@dataclass(frozen=True, slots=True)
class ChatBlobLink:
    """A ``[label](blob:<handle>)`` a message writes: the label and the handle."""

    label: str
    handle: str


def chat_blob_links(text: str) -> list[ChatBlobLink]:
    """Every held result ``text`` links to, in order, once per handle.

    The web transcript's rule for a ``blob:`` reference (``ChatResultLink``,
    reached from both its own-line block and its inline tokenizer): a
    ``[label](blob:<handle>)`` -- or the tolerated ``blob://<handle>`` -- read as
    a link, outside fenced code and math and not inside a code span or a bold
    run. A surface that cannot open the result itself opens the file the agent
    wrote it out to, which is why the box needs to know which ones a reply
    names.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[ChatBlobLink] = []
    seen: set[str] = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        closing = _FENCE_CLOSE if _FENCE_OPEN.match(line) else _MATH_FENCE
        if _FENCE_OPEN.match(line) or _MATH_FENCE.match(line):
            index += 1
            while index < len(lines) and not closing.match(lines[index]):
                index += 1
            index += 1
            continue
        index += 1
        for match in _INLINE.finditer(line):
            label, target = match.group(1), match.group(2)
            if label is None or target is None:
                continue
            if match.start() > 0 and line[match.start() - 1] == "!":
                continue
            blob = _BLOB.match(target)
            if blob is None:
                continue
            handle = target[blob.end() :].strip()
            if not handle or handle in seen:
                continue
            seen.add(handle)
            out.append(ChatBlobLink(label, handle))
    return out


@dataclass(frozen=True, slots=True)
class ReplyFile:
    """One file in the chat a reply shows or links, as the reader meets it.

    ``label`` is what the reader sees (the link text, the image's alt);
    ``target`` what the message wrote (a path, or ``blob:<handle>`` for a held
    result); ``path`` the file it names in the chat; ``handle`` the held result
    whose written-out file it is, when it is one.
    """

    label: str
    target: str
    path: ChatPath
    handle: str | None = None


def reply_files(
    text: str, *, chat_id: str, result_files: Mapping[str, str] | None = None
) -> list[ReplyFile]:
    """Every file in chat ``chat_id``'s folder a reply puts in front of a reader.

    The pictures (:func:`chat_images`) and the file links
    (:func:`chat_file_links`) the transcript turns into doors onto the chat's
    files, and every held result it links (:func:`chat_blob_links`) whose
    written-out file ``result_files`` knows (handle -> the path the tool
    reported). A result with no file there is not a file reference: the web
    shows it as its label and waits for nothing. Each file once, in order.
    """
    files: dict[str, str] = dict(result_files or {})
    out: list[ReplyFile] = []
    seen: set[tuple[str, str]] = set()

    def _add(found: ReplyFile) -> None:
        key = (found.path.anchor, found.path.path)
        if key not in seen:
            seen.add(key)
            out.append(found)

    for image in chat_images(text, chat_id=chat_id):
        _add(ReplyFile(image.alt, image.target, image.path))
    for link in chat_file_links(text, chat_id=chat_id):
        _add(ReplyFile(link.label, link.target, link.path))
    for blob in chat_blob_links(text):
        written = files.get(blob.handle)
        if written is None:
            continue
        found = chat_path(written, chat_id=chat_id)
        if found is not None:
            _add(ReplyFile(blob.label, f"blob:{blob.handle}", found, handle=blob.handle))
    return out


__all__ = [
    "CHAT_IMAGE_EXTENSIONS",
    "Anchor",
    "ChatBlobLink",
    "ChatImage",
    "ChatLink",
    "ChatPath",
    "ChatReference",
    "ReplyFile",
    "chat_blob_links",
    "chat_file_links",
    "chat_images",
    "chat_path",
    "chat_references",
    "reply_files",
]
