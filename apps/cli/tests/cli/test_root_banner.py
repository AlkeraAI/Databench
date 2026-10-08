"""The root help's banner: the wordmark, and the plain fallbacks."""

from __future__ import annotations

import io

from alkera_cli.ui import banner, theme
from rich.console import Console


def _console(width: int = 100) -> Console:
    return Console(
        file=io.StringIO(),
        force_terminal=True,
        width=width,
        theme=theme.build_theme("dark"),
    )


def _output(console: Console) -> str:
    file = console.file
    assert isinstance(file, io.StringIO)
    return file.getvalue()


def test_banner_rows_are_uniform_width() -> None:
    widths = {len(row) for row in banner.banner_rows()}
    assert widths == {len(banner.banner_rows()[0])}


def test_banner_renders_art_and_tagline() -> None:
    console = _console(width=100)
    banner.render_banner(console)
    out = _output(console)
    assert banner.banner_rows()[0] in out
    assert banner.banner_rows()[-1] in out
    assert banner.TAGLINE in out


def test_banner_narrow_terminal_falls_back_to_text() -> None:
    console = _console(width=30)
    banner.render_banner(console)
    out = _output(console)
    assert banner.plain_name() in out
    assert "█" not in out
    assert banner.TAGLINE in out


def test_banner_survives_cp1252_console_without_crashing() -> None:
    """A legacy/cp1252 console (old Windows ``cmd.exe``) can't encode the
    block-art wordmark. Regression: `alkera --help` rendered the banner and died
    with ``UnicodeEncodeError`` at flush, failing the win32 release smoke. The
    banner must degrade to plain ASCII, never raise.

    Fails without the guard (the block-art print raises), passes with it.
    """
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    console = Console(
        file=stream,
        force_terminal=True,
        width=100,  # >= banner_width → the block-art path (the one that crashed)
        theme=theme.build_theme("dark"),
    )
    banner.render_banner(console)  # must NOT raise
    stream.flush()
    out = raw.getvalue().decode("cp1252")
    assert banner.plain_name() in out  # plain-ASCII fallback rendered
    assert "█" not in out  # the un-encodable block art was not emitted
