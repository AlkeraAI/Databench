"""The ``alkera cloud-mirror run`` command surface: it needs a signed-in
machine, honours the API URL override, and roots the runtime at the
project's ``.alkera/``."""

from __future__ import annotations

import asyncio
import os
import platform
import signal
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import typer
from _help_text import HELP_ENV, plain
from alkera_cli.account import auth_file
from alkera_cli.account.auth_file import StoredAuth
from alkera_cli.cloud.service import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    MirrorSettings,
    machine_agent_id,
)
from alkera_cli.commands.box import (
    box_folders,
    build_mirror_runtime,
    install_stop_signals,
    mirror_settings_from_auth,
)
from alkera_cli.host import paths
from alkera_cli.main import app
from alkera_core.authz.headers import AGENT_ID_HEADER
from alkera_core.compute.liveness import MISSED_HEARTBEATS_BEFORE_UNREACHABLE
from alkera_core.config import settings
from typer.testing import CliRunner


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", home / "auth.yml")
    monkeypatch.delenv("ALKERA_API_URL", raising=False)
    monkeypatch.delenv("ALKERA_MACHINE_NAME", raising=False)
    for name in (
        "ALKERA_MACHINE_PROVIDER",
        "ALKERA_MACHINE_PROVIDER_POD_ID",
        "ALKERA_MACHINE_TYPE_CODE",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def registered_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two facts provisioning writes into the box's .env.demo."""
    monkeypatch.setenv("ALKERA_MACHINE_PROVIDER_POD_ID", "pod-env-1")
    monkeypatch.setenv("ALKERA_MACHINE_TYPE_CODE", "cpu3c")


def _signed_in() -> None:
    auth_file.save_auth(
        StoredAuth(
            api_url="http://login.test",
            token="device-jwt",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )


def test_run_refuses_when_the_machine_is_not_signed_in(home: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["cloud-mirror", "run", "--project", str(tmp_path)])
    assert result.exit_code == 2
    assert "alkera login" in result.output


def test_settings_come_from_the_login_with_the_url_override_winning(
    home: Path, registered_box: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in()
    monkeypatch.setattr("alkera_cli.commands.box._resolve_user_id", lambda *_: "user-1")
    monkeypatch.setenv("ALKERA_MACHINE_NAME", "demo-box")
    monkeypatch.setenv("ALKERA_TURN_WALL_CLOCK_SECONDS", "45")

    settings = mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name=None)
    assert settings.api_url == "http://login.test"  # the URL the token was issued against
    assert settings.token == "device-jwt"
    assert settings.machine_name == "demo-box"
    assert settings.user_id == "user-1"
    assert settings.budget.wall_clock_seconds == 45.0
    assert settings.project_dir == tmp_path.resolve()

    monkeypatch.setenv("ALKERA_API_URL", "http://env.test")
    from_env = mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name=None)
    assert from_env.api_url == "http://env.test"  # the explicit env override beats the login

    overridden = mirror_settings_from_auth(
        api_url="https://tunnel.example ", project_dir=tmp_path, machine_name="box 2"
    )
    assert overridden.api_url == "https://tunnel.example"  # the flag beats both
    assert overridden.machine_name == "box 2"


def test_settings_refuse_without_a_login(home: Path, tmp_path: Path) -> None:
    with pytest.raises(typer.BadParameter, match="alkera login"):
        mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name=None)


def test_the_registration_facts_come_from_the_env_and_the_flags_win(
    home: Path, registered_box: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in()
    monkeypatch.setattr("alkera_cli.commands.box._resolve_user_id", lambda *_: "user-1")
    from_env = mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name=None)
    assert (from_env.provider, from_env.provider_pod_id, from_env.machine_type_code) == (
        "self_hosted",
        "pod-env-1",
        "cpu3c",
    )
    monkeypatch.setenv("ALKERA_MACHINE_PROVIDER", "otherpod")
    from_flags = mirror_settings_from_auth(
        api_url=None,
        project_dir=tmp_path,
        machine_name=None,
        provider="runpod",
        provider_pod_id="pod-flag",
        machine_type_code="gpu1x",
    )
    assert (from_flags.provider, from_flags.provider_pod_id, from_flags.machine_type_code) == (
        "runpod",
        "pod-flag",
        "gpu1x",
    )
    assert from_flags.machine_identity.provider_pod_id == "pod-flag"


@pytest.mark.parametrize(
    ("env", "names"),
    [
        pytest.param({}, ["ALKERA_MACHINE_PROVIDER_POD_ID", "ALKERA_MACHINE_TYPE_CODE"], id="both"),
        pytest.param(
            {"ALKERA_MACHINE_TYPE_CODE": "cpu3c"}, ["ALKERA_MACHINE_PROVIDER_POD_ID"], id="pod-id"
        ),
        pytest.param(
            {"ALKERA_MACHINE_PROVIDER_POD_ID": "pod-1"},
            ["ALKERA_MACHINE_TYPE_CODE"],
            id="type-code",
        ),
    ],
)
def test_a_box_that_cannot_register_is_refused_before_anything_connects(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    names: list[str],
) -> None:
    """No pod id or no catalog code: the mirror would 422 on every
    registration and sit on chats it could never publish. Refused up front,
    with the env var and the flag named."""
    _signed_in()
    monkeypatch.setattr("alkera_cli.commands.box._resolve_user_id", lambda *_: "user-1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(typer.BadParameter) as refused:
        mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name=None)
    for name in names:
        assert name in str(refused.value)
    assert "cannot register" in str(refused.value)

    result = CliRunner().invoke(app, ["cloud-mirror", "run", "--project", str(tmp_path)])
    assert result.exit_code == 2, result.output
    for name in names:
        assert name in result.output


def test_the_run_command_exposes_the_registration_flags(home: Path) -> None:
    result = CliRunner().invoke(app, ["cloud-mirror", "run", "--help"], env=HELP_ENV)
    assert result.exit_code == 0, result.output
    offered = plain(result.output)
    for flag in ("--provider", "--provider-pod-id", "--machine-type-code"):
        assert flag in offered


def test_the_runtime_is_rooted_at_the_projects_alkera_dir(tmp_path: Path) -> None:
    runtime = build_mirror_runtime(tmp_path)
    assert runtime.project.path == (tmp_path / ".alkera").resolve()


def test_the_box_beats_faster_than_the_cloud_gives_up_on_it() -> None:
    """The two halves of the heartbeat contract, held together.

    The daemon's beat and the window the cloud judges it by are set in two
    different processes' settings; drifted apart, a perfectly healthy box is
    declared unreachable (the beat is slower than the window) or a dead one
    reads ready for minutes (the window is far wider than the beat). Neither
    side can see the other's number at runtime, so this is where they meet.
    """
    assert settings.compute_heartbeat_interval_seconds == DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    assert settings.compute_heartbeat_ready_seconds == (
        MISSED_HEARTBEATS_BEFORE_UNREACHABLE * settings.compute_heartbeat_interval_seconds
    )
    assert MISSED_HEARTBEATS_BEFORE_UNREACHABLE >= 2, (
        "one lost beat must not be enough to call a live box dead"
    )


def test_the_machine_name_default_is_readable_where_there_is_no_uname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.uname`` does not exist on Windows, and the daemon is packaged for
    it — a default that reaches for it would raise ``AttributeError`` there
    rather than name the box. The env override still wins when it is set."""
    monkeypatch.delenv("ALKERA_MACHINE_NAME", raising=False)
    monkeypatch.delattr(os, "uname", raising=False)
    assert MirrorSettings.machine_name_from_env({}) == (platform.node() or socket.gethostname())
    assert MirrorSettings.machine_name_from_env({"ALKERA_MACHINE_NAME": " box-9 "}) == "box-9"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "asyncio has no signal handlers on Windows (ProactorEventLoop."
        "add_signal_handler raises NotImplementedError) and a detached child is "
        "stopped by the Job Object / CTRL_BREAK_EVENT the spawn module uses, not "
        "by SIGTERM delivery. The Windows half of the contract -- install nothing, "
        "raise nothing -- is pinned by the test below."
    ),
)
async def test_a_stop_signal_asks_the_box_to_stop_instead_of_killing_it() -> None:
    """The supervisor stops a box with SIGTERM. Left to the platform default
    the process dies on it at once, before the service can push and release
    the chats it serves; installed, the signal flips the stop event and the
    run ends the way an idle sweep would."""
    stop = asyncio.Event()
    installed = install_stop_signals(stop)
    assert signal.SIGTERM in installed
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), timeout=5)
    finally:
        loop = asyncio.get_running_loop()
        for number in installed:
            loop.remove_signal_handler(number)
    assert stop.is_set()


async def test_a_loop_without_signal_handlers_installs_none_and_still_runs() -> None:
    """Windows: ``add_signal_handler`` raises ``NotImplementedError`` there, and
    a box is stopped by the Job Object kill the spawn module documents rather
    than by a signal the loop could catch. The run must still start -- so the
    refusal is swallowed, nothing is reported as installed, and the caller is
    left on the platform default instead of a crash in ``run``."""
    loop = asyncio.get_running_loop()
    asked: list[int] = []

    def refuse(number: int, *_: object) -> None:
        asked.append(number)
        raise NotImplementedError

    original = loop.add_signal_handler
    loop.add_signal_handler = refuse  # type: ignore[method-assign]
    try:
        installed = install_stop_signals(asyncio.Event())
    finally:
        loop.add_signal_handler = original  # type: ignore[method-assign]

    assert installed == []
    assert asked, "every stop signal is still offered to the loop"


def test_the_box_runs_with_custody_over_its_own_credential(tmp_path: Path) -> None:
    """``run`` hands the service a ChatFolders that can really lease — the
    SDK's Files namespace over the login's token — rather than the inert one
    the service builds for itself. That inert default is exactly how the
    lease path stayed dead code on every provisioned box."""
    settings = MirrorSettings(
        api_url="http://api.test",
        token="device-jwt",
        project_dir=tmp_path,
        machine_name="box",
        provider_pod_id="pod-1",
        machine_type_code="cpu3c",
    )
    folders = box_folders(settings, build_mirror_runtime(tmp_path))
    assert folders.enabled
    assert folders.local_root("chat-a") == tmp_path / ".alkera" / "chats" / "chat-a"


# --- a box on its own machine credential ---------------------------------------

CREDENTIAL = "alk_machine_" + "x" * 48


@pytest.fixture
def credential_box(monkeypatch: pytest.MonkeyPatch, registered_box: None) -> None:
    """What provisioning writes into a box's environment: the credential, the
    pod it runs on, the API it reaches — and no login anywhere."""
    monkeypatch.setenv("ALKERA_MACHINE_CREDENTIAL", CREDENTIAL)
    monkeypatch.setenv("ALKERA_API_URL", "http://api.test")
    monkeypatch.delenv("ALKERA_MACHINE_TYPE_CODE", raising=False)

    def _never(*_: object) -> str:
        raise AssertionError("a box on its credential resolves no user")

    monkeypatch.setattr("alkera_cli.commands.box._resolve_user_id", _never)


def test_a_box_on_its_credential_needs_no_login_and_presents_the_credential(
    home: Path, credential_box: None, tmp_path: Path
) -> None:
    """No ``auth.yml`` on the box: the credential is the bearer, there is no
    user behind it, the API comes from the environment (the flag winning), and
    the catalog code is not asked for — the credential says what the box is."""
    settings = mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name=None)
    assert settings.token == CREDENTIAL
    assert settings.machine_credential == CREDENTIAL
    assert settings.user_id == ""
    assert settings.api_url == "http://api.test"
    assert settings.machine_type_code == ""
    overridden = mirror_settings_from_auth(
        api_url="https://tunnel.example ", project_dir=tmp_path, machine_name=None
    )
    assert overridden.api_url == "https://tunnel.example"


def test_a_box_on_its_credential_ignores_a_login_on_its_disk(
    home: Path, credential_box: None, tmp_path: Path
) -> None:
    """An operator's login left on the box is not the box's to present: the
    credential is still the bearer, and the API is still the environment's,
    never the login's."""
    _signed_in()
    settings = mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name=None)
    assert settings.token == CREDENTIAL
    assert settings.api_url == "http://api.test"
    assert settings.user_id == ""


def test_a_box_on_its_credential_needs_the_api_url(
    home: Path, credential_box: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALKERA_API_URL")
    with pytest.raises(typer.BadParameter, match="ALKERA_API_URL"):
        mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name=None)


def test_a_box_on_its_credential_runs_its_folders_and_its_rest_client_on_it(
    home: Path, credential_box: None, tmp_path: Path
) -> None:
    """Every call the box makes rides the credential: the Files custody's
    client and the service's REST client both present it, and a 401 is the
    credential being refused — the client re-reads no file for a fresher
    bearer, even with an operator's login sitting on the disk. The credential
    is all the client sends: the box asserts no agent until the claim has told
    it which machine it is."""
    from alkera_cli.cloud.service import CloudMirrorService

    _signed_in()
    settings = mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name=None)
    runtime = build_mirror_runtime(
        tmp_path,
        on_machine_credential=True,
        api_url=settings.api_url,
        machine_credential=settings.machine_credential,
    )
    folders = box_folders(settings, runtime)
    assert folders.enabled
    assert folders._http is not None
    # The Files clients carry no bearer header of their own: each request is
    # signed by the credential's auth as it goes out.
    assert "authorization" not in folders._http.headers
    outgoing = folders._http.build_request("GET", f"{settings.api_url}/api/v1/me")
    signed = next(folders._http.auth.sync_auth_flow(outgoing))
    assert signed.headers["authorization"] == f"Bearer {CREDENTIAL}"
    service = CloudMirrorService(settings, runtime, folders=folders)
    assert service.rest.headers() == {"Authorization": f"Bearer {CREDENTIAL}"}
    assert service.rest.credential.refresh() is False, "a credential is never re-read off a file"
    assert service.rest.credential.token == CREDENTIAL


def test_a_box_on_a_login_still_re_reads_its_file_on_a_refusal(
    home: Path, registered_box: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The org's own box, on its operator's device token: a 401 sends it back
    to the credential file — to the SAME person's sign-in for the SAME org,
    picking up a re-login — and it asserts the machine name it will register
    as. A sign-in to another org made current meanwhile is never taken up."""
    from _profiles import ORG_A, ORG_B, make_jwt, store
    from alkera_cli.cloud.service import CloudMirrorService

    boot = make_jwt(org=ORG_A, exp=4102444800)
    store(ORG_A, current=True, token=boot, api_url="http://login.test")
    monkeypatch.setattr("alkera_cli.commands.box._resolve_user_id", lambda *_: "user-1")
    settings = mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name=None)
    assert settings.token == boot
    service = CloudMirrorService(settings, build_mirror_runtime(tmp_path))

    store(ORG_B, current=True, token=make_jwt(org=ORG_B), api_url="http://login.test")
    assert service.rest.credential.refresh() is False, "another org's sign-in is not the box's"
    assert service.rest.credential.token == boot

    relogin = make_jwt(org=ORG_A, exp=4102444801)
    store(ORG_A, token=relogin, api_url="http://login.test")
    assert service.rest.credential.refresh() is True
    assert service.rest.credential.token == relogin
    assert service.rest.headers()[AGENT_ID_HEADER] == machine_agent_id(settings.machine_name)


def test_the_machine_runtime_holds_no_login_of_its_own(
    home: Path, credential_box: None, tmp_path: Path
) -> None:
    """The runtime a box serves chats with: a judge, a resolver and web-tool
    flags that hold no credential until a chat's token is bound to them — with
    an operator's login on the disk, which none of them is built from."""
    from alkera_cli.cloud_sync.client import ChatConnectionsClient
    from alkera_cli.harness.org_flags import machine_web_flags
    from alkera_cli.harness.safety_judge import GatewaySafetyJudge
    from alkera_cli.harness.subagent_routing import TierModelResolver

    _signed_in()
    runtime = build_mirror_runtime(
        tmp_path,
        on_machine_credential=True,
        api_url="http://api.test",
        machine_credential=CREDENTIAL,
    )
    judge = runtime._safety_judge
    assert isinstance(judge, GatewaySafetyJudge)
    assert judge._token is None
    assert isinstance(runtime._subagent_model_resolver, TierModelResolver)
    assert runtime._web_search_enabled is machine_web_flags
    # Each chat's connection scope is the box's own chat-scoped door, on the
    # machine credential — never a login's; a local runtime has no scope.
    scope = runtime._connection_scope
    assert scope is not None
    scope_client = getattr(scope, "__self__", None)
    assert isinstance(scope_client, ChatConnectionsClient)
    assert scope_client._auth == {"Authorization": f"Bearer {CREDENTIAL}"}
    local = build_mirror_runtime(tmp_path)
    # Signed in, a login box has a judge, and it too holds no token of its own:
    # each chat binds its own credential to it.
    assert isinstance(local._safety_judge, GatewaySafetyJudge)
    assert local._safety_judge._token is None
    assert local._connection_scope is None
    # A machine-credential runtime with no door to scope chats by is refused:
    # built anyway, it would serve every chat the union of every org's rows.
    with pytest.raises(ValueError, match="serve the union"):
        build_mirror_runtime(tmp_path, on_machine_credential=True)
