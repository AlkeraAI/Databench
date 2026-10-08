"""The brand's opening banner: the wordmark and tagline the root help leads with."""

from __future__ import annotations

from alkera_core.brand import current_brand
from rich.console import Console
from rich.text import Text

TAGLINE = "the data engineering agent"


def banner_rows() -> tuple[str, ...]:
    """The brand's block-letter wordmark, rows of equal width."""
    return current_brand().terminal_wordmark


def plain_name() -> str:
    """The product name in capitals, for a terminal that cannot draw the art."""
    return current_brand().product_name.upper()


_ROW_SHADES = (
    "alk.brand_strong",
    "alk.brand_strong",
    "alk.brand",
    "alk.brand",
    "alk.brand",
)
"""Top rows slightly emphasized — a quiet vertical fade."""


def _console_can_encode(console: Console, sample: str) -> bool:
    """Whether the console's output code page can encode ``sample``.

    A legacy / cp1252 console (e.g. old Windows ``cmd.exe``) cannot encode the
    block-art wordmark at all; writing it crashes with ``UnicodeEncodeError`` at
    flush. ``console.file`` resolves ``sys.stdout`` lazily and Rich writes
    through that same stream, so its ``.encoding`` is exactly the code page that
    would do the encoding. An unknown encoding (``StringIO``, a pipe) is treated
    as UTF-8, which encodes everything.
    """
    encoding = getattr(console.file, "encoding", None) or "utf-8"
    try:
        sample.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def render_banner(console: Console) -> None:
    """Print the wordmark + tagline, shown once at open.

    Falls back to the plain product name when the block art would not render
    cleanly: either the terminal is narrower than the art (it would wrap into
    garbage), or the console's code page can't encode the block glyphs. The
    latter is a real crash, not just cosmetics — ``alkera --help`` on a legacy
    cp1252 Windows console died with ``UnicodeEncodeError`` at flush (it broke
    the win32 release smoke), so we choose ASCII BEFORE writing un-encodable
    bytes rather than catch the error after the buffer is already corrupt. The
    tagline is ASCII and always renders.
    """
    rows = banner_rows()
    art_ok = console.width >= len(rows[0]) and _console_can_encode(console, rows[0])
    console.print()
    if art_ok:
        for row, shade in zip(rows, _ROW_SHADES, strict=True):
            console.print(Text(row, style=shade))
    else:
        console.print(Text(plain_name(), style="alk.brand_strong"))
    console.print(Text(TAGLINE, style="alk.tertiary_text"))
    console.print()
