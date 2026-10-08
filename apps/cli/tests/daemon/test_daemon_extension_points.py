"""The daemon and the command tree take private code only by registration.

With no extension installed, the daemon serves only methods declared in open
modules and `alkera` mounts no data command, and the open protocol subset the
extension's codegen reads is exactly what that daemon serves. Contributions
(``DAEMON_METHODS``, ``CLI_COMMANDS``) are refused when they would shadow an
open command or borrow a code the server answers with itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import typer
from _helpers.daemon_wire import OPEN_SCHEMA, open_boot_report, rpc_error, schema_names
from alkera_cli.commands.extension_points import CommandMount, mount_commands
from alkera_cli.daemon.extension_points import DaemonMethods, RpcErrorAnswer
from alkera_cli.daemon.protocol import METHODS, _DaemonModel, method
from alkera_cli.daemon.server import (
    AUTH_REQUIRED,
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    RESERVED_CODES,
    SESSION_NOT_OPEN,
    contributed_error_answers,
)
from alkera_core.extensions import ExtensionError
from typer.testing import CliRunner

pytestmark = [pytest.mark.xdist_group("daemon_extension_points")]

#: Names the data product mounts; the open CLI answers none of them.
DATA_COMMAND_NAMES = ("context", "lineage", "gate", "tests", "test")


@pytest.fixture(scope="module")
def open_boot(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """What the daemon registers and the CLI mounts in a fresh interpreter that
    installs no extension."""
    return open_boot_report(tmp_path_factory.mktemp("open_home"))


def test_the_open_cli_mounts_no_data_command(open_boot: dict[str, Any]) -> None:
    """Click answers an unknown command with usage error 2; the open commands
    beside them still answer."""
    commands = open_boot["commands"]
    assert {name: commands[name] for name in DATA_COMMAND_NAMES} == dict.fromkeys(
        DATA_COMMAND_NAMES, 2
    )
    assert commands["login"] == 0
    assert commands["files"] == 0


def test_the_open_protocol_is_what_the_open_daemon_serves(open_boot: dict[str, Any]) -> None:
    names = schema_names(OPEN_SCHEMA)

    assert names["x-alkera-methods"] == set(open_boot["methods"])
    assert names["x-alkera-notifications"] == set(open_boot["notifications"])
    assert names["x-alkera-clientRequests"] == set(open_boot["client_requests"])


# --- error answers ---------------------------------------------------------------


class _DataError(Exception):
    pass


class _OtherDataError(Exception):
    pass


def _contribution(name: str, *answers: RpcErrorAnswer) -> DaemonMethods:
    return DaemonMethods(name=name, load=lambda: None, errors=answers)


@pytest.mark.parametrize("code", sorted(RESERVED_CODES))
def test_a_contribution_may_not_answer_under_a_reserved_code(code: int) -> None:
    with pytest.raises(ExtensionError, match="reserves"):
        contributed_error_answers((_contribution("data", RpcErrorAnswer(_DataError, code)),))


def test_two_contributions_may_not_answer_the_same_error() -> None:
    with pytest.raises(ExtensionError, match="both 'first' and 'second'"):
        contributed_error_answers(
            (
                _contribution("first", RpcErrorAnswer(_DataError, -32040)),
                _contribution("second", RpcErrorAnswer(_DataError, -32041)),
            )
        )


def test_answers_keep_installation_order_and_may_use_the_internal_error_code() -> None:
    first = RpcErrorAnswer(_DataError, -32040)
    second = RpcErrorAnswer(_OtherDataError, INTERNAL_ERROR)

    assert contributed_error_answers(
        (_contribution("first", first), _contribution("second", second))
    ) == (first, second)


def test_the_reserved_codes_cover_the_ones_a_client_keys_on() -> None:
    assert {AUTH_REQUIRED, SESSION_NOT_OPEN, METHOD_NOT_FOUND} <= RESERVED_CODES
    assert INTERNAL_ERROR not in RESERVED_CODES


class _RaiseRequest(_DaemonModel):
    pass


class _RaiseResponse(_DaemonModel):
    pass


@pytest.fixture
def unanswered_method() -> Iterator[str]:
    """A method raising an error no contribution answers."""
    name = "test.raise_unanswered"

    @method(name)
    async def _raise(server: Any, params: _RaiseRequest) -> _RaiseResponse:
        raise _DataError("boom")

    try:
        yield name
    finally:
        METHODS.pop(name, None)


async def test_an_error_nobody_answers_is_an_internal_fault(unanswered_method: str) -> None:
    error = await rpc_error(unanswered_method, {})

    assert error == {"code": INTERNAL_ERROR, "message": "_DataError: boom"}


# --- mounting commands -----------------------------------------------------------


def _tree() -> typer.Typer:
    app = typer.Typer()

    @app.command()
    def login() -> None:
        """Sign in."""

    files = typer.Typer()

    @files.command("push")
    def push() -> None:
        """Push."""

    app.add_typer(files, name="files")
    return app


def _group(echo: str) -> typer.Typer:
    group = typer.Typer()

    @group.command("show")
    def show() -> None:
        typer.echo(echo)

    @group.callback()
    def _callback() -> None:
        """A callback keeps this a multi-command group."""

    return group


def _command() -> None:
    typer.echo("ran")


def test_contributed_groups_and_commands_mount_beside_the_open_ones() -> None:
    app = _tree()
    mount_commands(
        app,
        (CommandMount("graph", group=_group("graph")), CommandMount("probe", command=_command)),
    )

    runner = CliRunner()
    assert runner.invoke(app, ["graph", "show"]).output == "graph\n"
    assert runner.invoke(app, ["probe"]).output == "ran\n"
    assert runner.invoke(app, ["login"]).exit_code == 0


@pytest.mark.parametrize(
    "mounts",
    [
        pytest.param((CommandMount("login", command=_command),), id="an-open-command"),
        pytest.param((CommandMount("files", group=_group("files")),), id="an-open-group"),
        pytest.param(
            (CommandMount("graph", group=_group("a")), CommandMount("graph", command=_command)),
            id="an-earlier-contribution",
        ),
    ],
)
def test_a_contribution_may_not_take_a_name_already_mounted(
    mounts: tuple[CommandMount, ...],
) -> None:
    with pytest.raises(ExtensionError, match="already part of the CLI"):
        mount_commands(_tree(), mounts)


def test_a_contribution_mounts_under_an_open_group() -> None:
    app = _tree()
    mount_commands(app, (CommandMount("probe", command=_command, parent="files"),))

    runner = CliRunner()
    assert runner.invoke(app, ["files", "probe"]).output == "ran\n"
    assert runner.invoke(app, ["files", "push"]).exit_code == 0
    assert runner.invoke(app, ["probe"]).exit_code == 2


@pytest.mark.parametrize(
    ("mounts", "message"),
    [
        pytest.param(
            (CommandMount("push", command=_command, parent="files"),),
            "already part of `files`",
            id="a-name-the-group-has",
        ),
        pytest.param(
            (
                CommandMount("probe", command=_command, parent="files"),
                CommandMount("probe", command=_command, parent="files"),
            ),
            "already part of `files`",
            id="an-earlier-contribution-to-the-group",
        ),
        pytest.param(
            (CommandMount("probe", command=_command, parent="ghost"),),
            "no command group 'ghost'",
            id="a-group-the-cli-does-not-have",
        ),
    ],
)
def test_a_contribution_under_a_group_is_refused_when_it_cannot_mount(
    mounts: tuple[CommandMount, ...], message: str
) -> None:
    with pytest.raises(ExtensionError, match=message):
        mount_commands(_tree(), mounts)


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({}, id="neither"),
        pytest.param({"group": typer.Typer(), "command": _command}, id="both"),
    ],
)
def test_a_mount_names_exactly_one_group_or_command(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        CommandMount("graph", **fields)
