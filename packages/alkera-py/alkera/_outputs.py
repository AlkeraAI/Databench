"""Output objects: Markdown, HTML, images, stacks and callouts.

Each one renders by itself in any front end that speaks the IPython display
protocol (``_repr_mimebundle_``: Jupyter, the Alkera kernel) or marimo's
(``_mime_``), with no host code. The HTML is self-contained: inline styles,
images as ``data:`` URLs, nothing fetched.
"""

from __future__ import annotations

import base64
import html
import os
import re
from collections.abc import Container, Iterable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict

from . import _host, _markdown

__all__ = [
    "Callout",
    "Html",
    "Image",
    "Markdown",
    "Output",
    "Stack",
    "callout",
    "hstack",
    "html_of",
    "image",
    "md",
    "vstack",
]

Bundle = Dict[str, Any]

CALLOUT_KINDS = ("neutral", "info", "success", "warn", "danger")
_CALLOUT_COLORS = {
    "neutral": ("rgba(120, 120, 120, 0.10)", "rgba(120, 120, 120, 0.55)"),
    "info": ("rgba(37, 99, 235, 0.10)", "rgba(37, 99, 235, 0.65)"),
    "success": ("rgba(22, 163, 74, 0.10)", "rgba(22, 163, 74, 0.65)"),
    "warn": ("rgba(217, 119, 6, 0.12)", "rgba(217, 119, 6, 0.70)"),
    "danger": ("rgba(220, 38, 38, 0.10)", "rgba(220, 38, 38, 0.70)"),
}


class _TextOf(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def text_of(markup: str) -> str:
    """The visible text of an HTML fragment (for ``text/plain``)."""
    parser = _TextOf()
    parser.feed(markup)
    parser.close()
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


class Output:
    """Base of every ``alkera`` output object."""

    def _html(self) -> str:
        raise NotImplementedError

    def _markdown(self) -> str | None:
        return None

    def _plain(self) -> str:
        return text_of(self._html())

    def _bundle(self) -> Bundle:
        bundle: Bundle = {"text/html": self._html()}
        markdown = self._markdown()
        if markdown is not None:
            bundle["text/markdown"] = markdown
        bundle["text/plain"] = self._plain()
        return bundle

    def _repr_mimebundle_(
        self, include: Container[str] | None = None, exclude: Container[str] | None = None
    ) -> Bundle:
        bundle = self._bundle()
        return {
            mime: value
            for mime, value in bundle.items()
            if (include is None or mime in include) and (exclude is None or mime not in exclude)
        }

    def _mime_(self) -> tuple[str, str]:
        return "text/html", self._html()

    def _repr_html_(self) -> str:
        return self._html()

    @property
    def text(self) -> str:
        """The HTML, as marimo's ``Html.text`` names it."""
        return self._html()

    def __repr__(self) -> str:
        plain = self._plain()
        short = plain if len(plain) <= 60 else plain[:57] + "..."
        return f"{type(self).__name__}({short!r})"


class Markdown(Output):
    def __init__(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError(f"alkera.md takes text, not {type(text).__name__}")
        self.source = _markdown.dedent(text)

    def _html(self) -> str:
        return f'<div class="alkera-md">{_markdown.render(self.source)}</div>'

    def _markdown(self) -> str:
        return self.source

    def _plain(self) -> str:
        return self.source


class Html(Output):
    def __init__(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError(f"alkera.html takes text, not {type(text).__name__}")
        self.source = text

    def _html(self) -> str:
        return self.source


_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_SVG = re.compile(
    rb"^\s*(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)*(?:<!DOCTYPE[^>]*>\s*)?<svg[\s>]",
    re.DOTALL | re.IGNORECASE,
)
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"})


def sniff(data: bytes) -> str | None:
    """The image type ``data`` starts with, or None."""
    for signature, mimetype in _SIGNATURES:
        if data.startswith(signature):
            return mimetype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if _SVG.match(data.lstrip(b"\xef\xbb\xbf")[:4096]):
        return "image/svg+xml"
    return None


class Image(Output):
    def __init__(
        self, data: bytes | str | os.PathLike[str], *, mimetype: str | None = None, alt: str = ""
    ) -> None:
        if isinstance(data, (str, os.PathLike)):
            raw = Path(data).read_bytes()
        elif isinstance(data, (bytes, bytearray, memoryview)):
            raw = bytes(data)
        else:
            raise TypeError(f"alkera.image takes bytes or a path, not {type(data).__name__}")
        kind = mimetype or sniff(raw)
        if kind is None:
            raise ValueError(
                "alkera.image could not tell the image type; "
                "pass mimetype= (PNG, JPEG, GIF, WebP, SVG)"
            )
        if kind not in IMAGE_TYPES:
            raise ValueError(f"alkera.image does not show {kind}; use PNG, JPEG, GIF, WebP or SVG")
        self.data = raw
        self.mimetype = kind
        self.alt = alt

    def data_url(self) -> str:
        return f"data:{self.mimetype};base64,{base64.b64encode(self.data).decode('ascii')}"

    def _html(self) -> str:
        # SVG goes through <img> too, so scripts inside it never run.
        return _img(self.data_url(), self.alt)

    def _plain(self) -> str:
        return f"<{self.mimetype} image, {len(self.data)} bytes>"

    def _bundle(self) -> Bundle:
        bundle = super()._bundle()
        if self.mimetype == "image/svg+xml":
            bundle[self.mimetype] = self.data.decode("utf-8", errors="replace")
        else:
            bundle[self.mimetype] = base64.b64encode(self.data).decode("ascii")
        return bundle


def _img(src: str, alt: str = "") -> str:
    return f'<img src="{html.escape(src)}" alt="{html.escape(alt)}" style="max-width: 100%;" />'


def html_of(item: Any) -> str:
    """HTML for anything placed inside a stack or a callout: ``alkera``
    outputs as themselves, text as Markdown, anything else through the
    current host's formatter."""
    if isinstance(item, Output):
        return item._html()
    if isinstance(item, str):
        return _markdown.render(_markdown.dedent(item))
    bundle = _host.current().format(item)
    value = bundle.get("text/html")
    if isinstance(value, str):
        return value
    for mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        value = bundle.get(mime)
        if isinstance(value, str):
            return _img(f"data:{mime};base64,{value.strip()}")
    value = bundle.get("image/svg+xml")
    if isinstance(value, str):
        return Image(value.encode("utf-8"), mimetype="image/svg+xml")._html()
    value = bundle.get("text/markdown")
    if isinstance(value, str):
        return _markdown.render(value)
    plain = bundle.get("text/plain")
    return f"<pre>{html.escape(plain if isinstance(plain, str) else repr(item))}</pre>"


def _plain_of(item: Any) -> str:
    if isinstance(item, Output):
        return item._plain()
    if isinstance(item, str):
        return _markdown.dedent(item)
    plain = _host.current().format(item).get("text/plain")
    return plain if isinstance(plain, str) else repr(item)


class Stack(Output):
    def __init__(self, items: Iterable[Any], *, direction: str, gap: float = 0.5) -> None:
        if isinstance(items, (str, bytes)) or not isinstance(items, Iterable):
            raise TypeError("alkera.hstack and alkera.vstack take a list of items")
        self.items = list(items)
        self.direction = direction
        self.gap = float(gap)

    def _html(self) -> str:
        flow = "row" if self.direction == "h" else "column"
        wrap = " flex-wrap: wrap; align-items: flex-start;" if self.direction == "h" else ""
        style = f"display: flex; flex-direction: {flow}; gap: {self.gap:g}rem;{wrap}"
        children = "".join(
            f'<div style="min-width: 0;">{html_of(item)}</div>' for item in self.items
        )
        return f'<div class="alkera-stack" style="{style}">{children}</div>'

    def _plain(self) -> str:
        sep = "  " if self.direction == "h" else "\n"
        return sep.join(_plain_of(item) for item in self.items)


class Callout(Output):
    def __init__(self, obj: Any, kind: str = "neutral") -> None:
        if kind not in CALLOUT_KINDS:
            raise ValueError(
                f"alkera.callout kind must be one of {', '.join(CALLOUT_KINDS)}, not {kind!r}"
            )
        self.obj = obj
        self.kind = kind

    def _html(self) -> str:
        background, border = _CALLOUT_COLORS[self.kind]
        style = (
            f"background: {background}; border-left: 4px solid {border}; border-radius: 6px; "
            "padding: 0.6rem 0.9rem; margin: 0.25rem 0;"
        )
        kind = f"alkera-callout alkera-callout-{self.kind}"
        return f'<div class="{kind}" role="note" style="{style}">{html_of(self.obj)}</div>'

    def _markdown(self) -> str | None:
        if isinstance(self.obj, (str, Markdown)):
            body = self.obj.source if isinstance(self.obj, Markdown) else _markdown.dedent(self.obj)
            return "\n".join(
                f"> {line}" if line else ">"
                for line in [f"**{self.kind.capitalize()}**", "", *body.split("\n")]
            )
        return None

    def _plain(self) -> str:
        return f"[{self.kind}] {_plain_of(self.obj)}"


def md(text: str) -> Markdown:
    """Markdown, rendered to HTML (raw HTML in the text is escaped)."""
    return Markdown(text)


def html_(text: str) -> Html:
    """Raw HTML, shown as is."""
    return Html(text)


def image(data: bytes | str | os.PathLike[str], *, mimetype: str | None = None) -> Image:
    """An image from bytes or a file path; PNG, JPEG, GIF, WebP and SVG are
    recognised from their content."""
    return Image(data, mimetype=mimetype)


def hstack(items: Iterable[Any], *, gap: float = 0.5) -> Stack:
    """``items`` side by side (wrapping when narrow)."""
    return Stack(items, direction="h", gap=gap)


def vstack(items: Iterable[Any], *, gap: float = 0.5) -> Stack:
    """``items`` one above the other."""
    return Stack(items, direction="v", gap=gap)


def callout(obj: Any, kind: str = "neutral") -> Callout:
    """``obj`` in a coloured box: ``neutral``, ``info``, ``success``,
    ``warn`` or ``danger``."""
    return Callout(obj, kind)
