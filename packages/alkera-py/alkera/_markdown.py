"""A small Markdown to HTML renderer, standard library only.

It covers what notebook prose needs: ATX headings, paragraphs, emphasis,
strikethrough, code spans, fenced code, ordered and unordered lists (nested
by indentation), block quotes, horizontal rules, links, images and hard line
breaks. Everything else is text: raw HTML in the source is escaped, never
passed through, and a link or image whose URL names a scheme outside a short
allow list (``javascript:``, ``vbscript:``, ``data:`` other than raster
images, ...) is rendered as its text only.
"""

from __future__ import annotations

import html
import re
import textwrap

__all__ = ["render", "safe_url"]

_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
_DATA_IMAGE = re.compile(r"^data:image/(?:png|jpeg|gif|webp);base64,[a-z0-9+/=\s]*$", re.IGNORECASE)
_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.IGNORECASE)
# Control characters and whitespace a browser strips while parsing a URL,
# which would otherwise hide a scheme ("java\tscript:").
_URL_NOISE = re.compile(r"[\x00-\x20\x7f]")

_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*([^`\s]*)[^`]*$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_RULE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_BULLET = re.compile(r"^( {0,3})([-*+])([ \t]+)(.*)$")
_ORDERED = re.compile(r"^( {0,3})(\d{1,9})([.)])([ \t]+)(.*)$")
_QUOTE = re.compile(r"^ {0,3}> ?(.*)$")

_HOLD = "\x00"
_HOLD_RE = re.compile(r"\x00(\d+)\x00")
_ESCAPABLE = r"\\`*_{}\[\]()#+\-.!|~<>\""


def safe_url(url: str, *, image: bool = False) -> str | None:
    """``url`` when it is safe to put in an ``href`` (or ``src`` with
    ``image``), else None. Relative URLs and fragments are allowed."""
    cleaned = _URL_NOISE.sub("", html.unescape(url))
    if image and _DATA_IMAGE.match(cleaned):
        return url.strip()
    match = _SCHEME.match(cleaned)
    if match is None:
        # "/path", "page.html", "#anchor", "?q=1": no scheme, so the page's own.
        return url.strip()
    if match.group(1).lower() in _LINK_SCHEMES:
        return url.strip()
    return None


def render(text: str) -> str:
    """The HTML for Markdown ``text``."""
    source = text.replace("\r\n", "\n").replace("\r", "\n").replace(_HOLD, "\ufffd").expandtabs(4)
    return _blocks(source.split("\n"))


# --------------------------------------------------------------------------- blocks


def _blocks(lines: list[str]) -> str:
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = _FENCE.match(line)
        if fence:
            i = _fenced(lines, i, fence, out)
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline((heading.group(2) or '').strip())}</h{level}>")
            i += 1
            continue
        if _RULE.match(line):
            out.append("<hr />")
            i += 1
            continue
        if _QUOTE.match(line):
            quoted: list[str] = []
            while i < n:
                match = _QUOTE.match(lines[i])
                if match is None:
                    break
                quoted.append(match.group(1))
                i += 1
            out.append(f"<blockquote>\n{_blocks(quoted)}\n</blockquote>")
            continue
        if _BULLET.match(line) or _ORDERED.match(line):
            i = _list(lines, i, out)
            continue
        if line.startswith("    "):
            i = _indented_code(lines, i, out)
            continue
        i = _paragraph(lines, i, out)
    return "\n".join(out)


def _fenced(lines: list[str], i: int, fence: re.Match[str], out: list[str]) -> int:
    indent = len(fence.group(1))
    marker = fence.group(2)
    lang = fence.group(3)
    body: list[str] = []
    i += 1
    closing = re.compile(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$")
    while i < len(lines) and not closing.match(lines[i]):
        line = lines[i]
        strip = min(indent, len(line) - len(line.lstrip(" ")))
        body.append(line[strip:])
        i += 1
    attr = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
    code = html.escape("\n".join(body) + ("\n" if body else ""), quote=False)
    out.append(f"<pre><code{attr}>{code}</code></pre>")
    return i + 1


def _indented_code(lines: list[str], i: int, out: list[str]) -> int:
    body: list[str] = []
    while i < len(lines) and (lines[i].startswith("    ") or not lines[i].strip()):
        body.append(lines[i][4:])
        i += 1
    while body and not body[-1].strip():
        body.pop()
    out.append(f"<pre><code>{html.escape(chr(10).join(body) + chr(10), quote=False)}</code></pre>")
    return i


def _starts_block(line: str) -> bool:
    return bool(
        _FENCE.match(line) or _HEADING.match(line) or _RULE.match(line) or _QUOTE.match(line)
    )


def _paragraph(lines: list[str], i: int, out: list[str]) -> int:
    para: list[str] = []
    while i < len(lines) and lines[i].strip():
        line = lines[i]
        if para and (_starts_block(line) or _BULLET.match(line) or _ORDERED.match(line)):
            break
        para.append(line)
        i += 1
    out.append(f"<p>{_inline(_join_paragraph(para))}</p>")
    return i


def _join_paragraph(para: list[str]) -> str:
    joined: list[str] = []
    for index, line in enumerate(para):
        last = index == len(para) - 1
        if not last and (line.endswith("  ") or line.endswith("\\")):
            stripped = line.rstrip(" ")
            if stripped.endswith("\\") and not line.endswith("  "):
                stripped = stripped[:-1]
            joined.append(stripped.strip() + _HOLD + "br" + _HOLD)
        else:
            joined.append(line.strip())
    return "\n".join(joined)


def _item_marker(line: str) -> tuple[str, int, str, str] | None:
    """(kind, content column, start number, first line) for a list item."""
    bullet = _BULLET.match(line)
    if bullet:
        width = len(bullet.group(1)) + 1 + len(bullet.group(3))
        return "ul", width, "", bullet.group(4)
    ordered = _ORDERED.match(line)
    if ordered:
        width = len(ordered.group(1)) + len(ordered.group(2)) + 1 + len(ordered.group(4))
        return "ol", width, ordered.group(2), ordered.group(5)
    return None


def _list(lines: list[str], i: int, out: list[str]) -> int:
    first = _item_marker(lines[i])
    assert first is not None
    kind = first[0]
    start = first[2]
    items: list[list[str]] = []
    loose = False
    n = len(lines)
    while i < n:
        marker = _item_marker(lines[i])
        if marker is None or marker[0] != kind:
            break
        _, column, _, content = marker
        item = [content]
        i += 1
        while i < n:
            line = lines[i]
            if not line.strip():
                # A blank line continues the item only when indented content follows.
                j = i
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and len(lines[j]) - len(lines[j].lstrip(" ")) >= column:
                    item.extend([""] * (j - i))
                    loose = True
                    i = j
                    continue
                after = _item_marker(lines[j]) if j < n else None
                if after is not None and after[0] == kind:
                    loose = True
                break
            indent = len(line) - len(line.lstrip(" "))
            if indent >= column:
                item.append(line[column:])
            elif _item_marker(line) is not None or _starts_block(line):
                break
            else:
                item.append(line.strip())  # lazy continuation of the paragraph
            i += 1
        items.append(item)
        if i < n and not lines[i].strip():
            j = i
            while j < n and not lines[j].strip():
                j += 1
            following = _item_marker(lines[j]) if j < n else None
            if following is None or following[0] != kind:
                break
            loose = True
            i = j
    rendered: list[str] = []
    for item in items:
        body = _blocks(item)
        if not loose and body.startswith("<p>"):
            # A tight item: its first paragraph is not wrapped.
            end = body.find("</p>")
            body = body[3:end] + body[end + 4 :]
        rendered.append(f"<li>{body}</li>")
    attr = f' start="{int(start)}"' if kind == "ol" and start and int(start) != 1 else ""
    out.append(f"<{kind}{attr}>\n" + "\n".join(rendered) + f"\n</{kind}>")
    return i


# --------------------------------------------------------------------------- inline


class _Tokens:
    """Rendered HTML fragments held out of the text while the rest is
    escaped and emphasised, then put back."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def hold(self, fragment: str) -> str:
        self.parts.append(fragment)
        return f"{_HOLD}{len(self.parts) - 1}{_HOLD}"

    def plain(self, text: str) -> str:
        """``text`` with its fragments put back as plain text (for an
        attribute value or a URL check)."""
        return html.unescape(_TAG.sub("", self.restore(text)))

    def restore(self, text: str) -> str:
        for _ in range(64):
            if _HOLD not in text:
                break
            text = _HOLD_RE.sub(lambda m: self.parts[int(m.group(1))], text)
        return text


_TAG = re.compile(r"<[^>]*>")
_CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)", re.DOTALL)
_ESCAPE = re.compile(rf"\\([{_ESCAPABLE}])")
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^\s<>]+)>", re.IGNORECASE)
_LINK = re.compile(
    r"(!?)\[((?:[^\[\]\\]|\\.)*)\]\(\s*(<[^<>\n]*>|[^\s()<>]*(?:\([^\s()<>]*\)[^\s()<>]*)*)"
    r"(?:\s+(\"[^\"]*\"|'[^']*'))?\s*\)"
)
_STRONG_EM = re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", re.DOTALL)
_STRONG = re.compile(
    r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])", re.DOTALL
)
_EM = re.compile(
    r"\*(?=[^\s*])(.+?)(?<=[^\s*])\*|(?<![\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w])", re.DOTALL
)
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)


def _inline(text: str) -> str:
    tokens = _Tokens()
    text = text.replace(f"{_HOLD}br{_HOLD}", tokens.hold("<br />"))

    def code(m: re.Match[str]) -> str:
        content = m.group(2).replace("\n", " ")
        if content.startswith(" ") and content.endswith(" ") and content.strip():
            content = content[1:-1]
        return tokens.hold(f"<code>{html.escape(content)}</code>")

    text = _CODE_SPAN.sub(code, text)
    text = _ESCAPE.sub(lambda m: tokens.hold(html.escape(m.group(1))), text)

    def autolink(m: re.Match[str]) -> str:
        url = tokens.plain(m.group(1))
        if safe_url(url) is None:
            return tokens.hold(html.escape(url))
        return tokens.hold(f'<a href="{html.escape(url)}">{html.escape(url)}</a>')

    text = _AUTOLINK.sub(autolink, text)

    def link(m: re.Match[str]) -> str:
        bang, label, url, title = m.group(1), m.group(2), m.group(3), m.group(4)
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1]
        safe = safe_url(tokens.plain(url), image=bool(bang))
        title_attr = f' title="{html.escape(tokens.plain(title[1:-1]))}"' if title else ""
        if bang:
            alt = html.escape(tokens.plain(label))
            if safe is None:
                return tokens.hold(alt)
            return tokens.hold(f'<img src="{html.escape(safe)}" alt="{alt}"{title_attr} />')
        inner = _emphasis(html.escape(label, quote=False))
        if safe is None:
            return tokens.hold(inner)
        return tokens.hold(f'<a href="{html.escape(safe)}"{title_attr}>{inner}</a>')

    text = _LINK.sub(link, text)
    text = html.escape(text, quote=False)
    text = _emphasis(text)
    return tokens.restore(text)


def _emphasis(text: str) -> str:
    for _ in range(8):
        before = text
        text = _STRONG_EM.sub(lambda m: f"<strong><em>{m.group(1)}</em></strong>", text)
        text = _STRONG.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
        text = _STRIKE.sub(lambda m: f"<del>{m.group(1)}</del>", text)
        text = _EM.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
        if text == before:
            break
    return text


def dedent(text: str) -> str:
    """Triple-quoted notebook prose without its common indentation or the
    blank first and last lines."""
    return textwrap.dedent(text).strip("\n")
