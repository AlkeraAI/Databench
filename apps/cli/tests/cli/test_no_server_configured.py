"""A CLI with no API URL configured falls back to the local self-hosted stack.
When nothing answers there, it says to set ``ALKERA_API_URL`` instead of
printing the socket error. A URL somebody configured keeps its own error.

The local default is pointed at a port nothing listens on, the dotenv layer is
kept out by running from an empty directory, and the system config at a file
that does not exist."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli import main as cli_main
from alkera_cli.account import auth_file
from alkera_cli.commands import account as account_command
from alkera_cli.host import build_profile, config, endpoints, paths
from alkera_core.extensions import ExtensionPoint
from typer.testing import CliRunner

runner = CliRunner()


def _closed_port() -> int:
    """A localhost port nothing listens on: bound to learn a free one, then
    closed before anything dials it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def default_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[str]:
    """The local default, at a closed port, with nothing configured over it."""
    url = f"http://127.0.0.1:{_closed_port()}"
    monkeypatch.setattr(endpoints, "DEFAULT_API_URL", url)
    monkeypatch.setattr(build_profile, "API_URL", "")
    monkeypatch.delenv("ALKERA_API_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "SYSTEM_CONFIG_PATH", tmp_path / "no-system-config.yml")
    home = tmp_path / "alkera-home"
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "AUTH_FILE_PATH", home / "auth.yml")
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", home / "auth.yml")
    # Another test may have reloaded ``config``, leaving the command modules on
    # an older ``get_settings`` with its own cache.
    for module in (config, cli_main, account_command):
        module.get_settings.cache_clear()
    yield url
    for module in (config, cli_main, account_command):
        module.get_settings.cache_clear()


def _refusal(url: str) -> str:
    return (
        f"No server is configured and nothing answers at {url}. "
        "Set ALKERA_API_URL to your server's address, or start the local stack."
    )


def _said(output: str) -> str:
    """The output as one line, so a wrapped sentence still matches."""
    return " ".join(output.split())


def test_login_with_nothing_configured_and_no_local_stack_names_alkera_api_url(
    default_url: str,
) -> None:
    result = runner.invoke(cli_main.app, ["login", "--force"])
    assert result.exit_code == 1
    assert _refusal(default_url) in _said(result.stdout)
    assert "could not start device login" not in result.stdout


def test_login_against_a_configured_url_that_is_down_keeps_its_own_error(
    default_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same closed port, but chosen by ALKERA_API_URL."""
    monkeypatch.setenv("ALKERA_API_URL", default_url)
    result = runner.invoke(cli_main.app, ["login", "--force"])
    assert result.exit_code == 1
    assert "could not start device login" in result.stdout
    assert "ALKERA_API_URL" not in result.stdout


@pytest.mark.parametrize(
    "configure",
    [
        pytest.param("env", id="env"),
        pytest.param("system-config", id="system-config"),
        pytest.param("baked", id="release-build"),
    ],
)
def test_a_configured_url_is_never_refused_as_unconfigured(
    default_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configure: str
) -> None:
    if configure == "env":
        monkeypatch.setenv("ALKERA_API_URL", default_url)
    elif configure == "system-config":
        system = tmp_path / "system.yml"
        system.write_text(f"api_url: {default_url}\n", encoding="utf-8")
        monkeypatch.setattr(config, "SYSTEM_CONFIG_PATH", system)
    else:
        monkeypatch.setattr(build_profile, "API_URL", default_url)
    settings = config.get_settings()
    assert settings.alkera_api_url == default_url
    assert config.no_server_refusal(_refused_at(default_url), settings) is None


def _refused_at(url: str) -> httpx.ConnectError:
    return httpx.ConnectError("refused", request=httpx.Request("POST", f"{url}/api/v1/x"))


@pytest.mark.parametrize(
    ("make", "refused"),
    [
        pytest.param(lambda url: _refused_at(url), True, id="connect-error-at-the-default"),
        pytest.param(
            lambda url: httpx.ConnectTimeout(
                "slow", request=httpx.Request("GET", f"{url}/health/live")
            ),
            True,
            id="connect-timeout-at-the-default",
        ),
        pytest.param(
            lambda url: RuntimeError("could not start device login"),
            False,
            id="no-connect-failure",
        ),
        pytest.param(
            lambda url: httpx.ReadTimeout("slow", request=httpx.Request("GET", url)),
            False,
            id="a-server-that-answered-slowly",
        ),
        pytest.param(
            lambda url: _refused_at("http://127.0.0.1:1"),
            False,
            id="another-port",
        ),
        pytest.param(
            lambda url: _refused_at(url.replace("http://", "https://")),
            False,
            id="another-scheme",
        ),
        pytest.param(lambda url: httpx.ConnectError("no request"), False, id="no-request"),
    ],
)
def test_only_a_refused_connection_to_the_default_is_refused(
    default_url: str, make: Any, refused: bool
) -> None:
    exc: BaseException = make(default_url)
    said = config.no_server_refusal(exc, config.get_settings())
    assert said == (_refusal(default_url) if refused else None)


def test_a_connect_failure_wrapped_by_a_command_is_still_found(default_url: str) -> None:
    try:
        try:
            raise _refused_at(default_url)
        except httpx.ConnectError as inner:
            raise ValueError("the command's own words") from inner
    except ValueError as outer:
        said = config.no_server_refusal(outer, config.get_settings())
    assert said == _refusal(default_url)


def test_an_unhandled_refusal_at_the_default_is_no_crash(
    default_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[BaseException] = []
    hooks: ExtensionPoint[Any] = ExtensionPoint("cli_crash")
    hooks.register(seen.append)
    monkeypatch.setattr(cli_main, "CLI_CRASH", hooks)

    cli_main._report_unexpected(_refused_at(default_url))

    out = _said(capsys.readouterr().out)
    assert _refusal(default_url) in out
    assert "unexpected error" not in out
    assert seen == []
