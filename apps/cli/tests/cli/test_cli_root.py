"""The root command: `--help` is the banner screen, and bare `alkera` runs the one
default command a distribution registers, or shows the help."""

from __future__ import annotations

from typing import Any

import click
import pytest
import typer
import typer.rich_utils as rich_utils
from alkera_cli import main
from alkera_cli.commands.extension_points import CLI_COMMANDS, default_command
from alkera_cli.main import app
from alkera_cli.ui import DARK, LIGHT, apply_typer_brand
from alkera_cli.ui.banner import TAGLINE, banner_rows
from alkera_core.extensions import ExtensionError, ExtensionPoint
from typer.testing import CliRunner

runner = CliRunner()


def _first(project: Any) -> None:
    """A stand-in default command."""


def _second(project: Any) -> None:
    """Another stand-in default command."""


def test_no_default_command_when_none_is_registered() -> None:
    assert default_command(()) is None


def test_the_one_registered_default_command_is_the_default() -> None:
    assert default_command((_first,)) is _first


def test_two_default_commands_are_refused() -> None:
    with pytest.raises(ExtensionError, match="more than one default command"):
        default_command((_first, _second))


def test_help_flag_shows_the_banner() -> None:
    """`alkera --help` leads with the wordmark, then the command help."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert banner_rows()[4].strip() in result.output  # banner present
    assert TAGLINE in result.output
    assert "Usage" in result.output
    # A user-facing command is listed...
    assert "login" in result.output
    # ...while plumbing commands are hidden from the listing.
    for name in ("serve", "health", "cloud-mirror"):
        assert name not in result.output


def test_only_user_facing_commands_are_visible() -> None:
    """Exactly the open CLI's user-facing commands appear in `--help`, beside the
    ones an installed distribution mounts visibly; everything else is hidden
    (still runnable — see test_hidden_commands_still_invokable)."""
    group = typer.main.get_command(app)
    assert isinstance(group, click.Group)
    visible = {name for name, sub in group.commands.items() if not sub.hidden}
    contributed = {m.name for m in CLI_COMMANDS.items() if m.parent is None and not m.hidden}
    assert visible - contributed == {"login", "logout", "whoami", "org", "version"}


@pytest.mark.parametrize(
    "name", ["serve", "health", "run", "project", "files", "env", "cloud-mirror"]
)
def test_hidden_commands_still_invokable(name: str) -> None:
    """`hidden=True` only drops a command from `--help`; it must stay runnable.
    `<cmd> --help` exits 0 without side effects, so it proves the command is still
    registered."""
    result = runner.invoke(app, [name, "--help"])
    assert result.exit_code == 0


def test_subcommands_unaffected_by_root_callback() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "alkera-cli" in result.output


def test_apply_typer_brand_points_styles_at_palette() -> None:
    try:
        apply_typer_brand("light")
        assert rich_utils.STYLE_OPTION == f"bold {LIGHT.brand}"
        assert rich_utils.STYLE_COMMANDS_PANEL_BORDER == LIGHT.outline
        assert rich_utils.STYLE_HELPTEXT == LIGHT.tertiary_text
    finally:
        apply_typer_brand("dark")
    assert rich_utils.STYLE_OPTION == f"bold {DARK.brand}"


def test_an_unexpected_error_is_printed_then_handed_to_the_crash_hooks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[BaseException] = []
    hooks: ExtensionPoint[Any] = ExtensionPoint("cli_crash")
    hooks.register(seen.append)
    monkeypatch.setattr(main, "CLI_CRASH", hooks)
    boom = RuntimeError("the [red]index[/red] is gone")

    main._report_unexpected(boom)

    out = capsys.readouterr().out
    assert "alkera hit an unexpected error." in out
    # The message is printed as text, never read as markup.
    assert "RuntimeError: the [red]index[/red] is gone" in out
    assert seen == [boom]
