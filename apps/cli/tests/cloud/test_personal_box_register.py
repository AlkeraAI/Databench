"""``alkera cloud-mirror register``: a person's own box through the device flow.

The box runs the device flow as ``alkera-box``, receives a MACHINE credential,
claims its machine on it, and keeps the credential in a 0600 record. It never
writes a login: ``auth.yml`` must not exist afterwards, and anything the server
answers that is not a machine credential is refused with nothing written.
``cloud-mirror run`` then runs on the record, without reading a login.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.account import auth_file, device_flow
from alkera_cli.cloud import personal_box
from alkera_cli.cloud.personal_box import (
    PersonalBoxError,
    PersonalBoxRecord,
    load_personal_box,
    register_personal_box,
)
from alkera_cli.commands.box import mirror_settings_from_auth
from alkera_cli.host import paths
from alkera_cli.main import app
from typer.testing import CliRunner

API = "http://box.test"
CREDENTIAL = "alk_machine_" + "x" * 64
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "personal_box"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", home / "auth.yml")
    for name in (
        "ALKERA_API_URL",
        "ALKERA_MACHINE_NAME",
        "ALKERA_MACHINE_CREDENTIAL",
        "ALKERA_MACHINE_PROVIDER",
        "ALKERA_MACHINE_PROVIDER_POD_ID",
        "ALKERA_MACHINE_TYPE_CODE",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


class Backend:
    """The three calls the flow makes, scripted: the code, the polls, the claim."""

    def __init__(
        self,
        *,
        token: dict[str, Any] | None = None,
        claim_status: int = 201,
        pending_polls: int = 1,
    ) -> None:
        self.token = token if token is not None else {"access_token": CREDENTIAL}
        self.claim_status = claim_status
        self.pending = pending_polls
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/device/code":
            self.seen.append((path, dict(httpx.QueryParams(request.content.decode()))))
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-1",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": f"{API}/device",
                    "verification_uri_complete": f"{API}/device?user_code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 1,
                },
            )
        if path == "/api/v1/auth/device/token":
            self.seen.append((path, dict(httpx.QueryParams(request.content.decode()))))
            if self.pending:
                self.pending -= 1
                return httpx.Response(400, json={"error": "authorization_pending"})
            if "error" in self.token:
                return httpx.Response(400, json=self.token)
            return httpx.Response(200, json=self.token)
        if path == "/api/v1/machines/claim":
            self.seen.append(
                (
                    path,
                    {
                        "authorization": request.headers.get("authorization", ""),
                        **json.loads(request.content),
                    },
                )
            )
            return httpx.Response(self.claim_status, json={"id": "machine-1"})
        return httpx.Response(404)


def _register(backend: Backend, **overrides: Any) -> PersonalBoxRecord:
    transport = httpx.MockTransport(backend)
    kwargs: dict[str, Any] = {
        "name": "my laptop",
        "daemon_version": "9.9.9",
        "announce": lambda _code: None,
        "transport": transport,
        "claim_transport": transport,
        "sleep": lambda _s: None,
        **overrides,
    }
    return register_personal_box(API, **kwargs)


def test_the_box_registers_claims_on_its_credential_and_keeps_no_login(home: Path) -> None:
    backend = Backend()
    shown: list[str] = []
    record = _register(backend, announce=lambda code: shown.append(code.user_code))

    assert shown == ["ABCD-EFGH"]
    code_call, *polls, claim = backend.seen
    assert code_call[1]["client_id"] == device_flow.CLIENT_ID_BOX
    assert {poll[1]["client_id"] for poll in polls} == {device_flow.CLIENT_ID_BOX}
    assert claim[1]["authorization"] == f"Bearer {CREDENTIAL}"
    assert claim[1]["provider_pod_id"] == record.provider_pod_id
    assert claim[1]["name"] == "my laptop"

    stored = home / "personal_box.json"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert load_personal_box() == record
    assert (record.credential, record.machine_id, record.api_url) == (CREDENTIAL, "machine-1", API)
    assert record.provider_pod_id.startswith("personal-")
    assert not (home / "auth.yml").exists()
    assert CREDENTIAL not in repr(record)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param({"access_token": "eyJhbGciOiJIUzI1NiJ9.e30.sig"}, id="a-session-jwt"),
        pytest.param({"access_token": "alk_machine_org.eyJ.x.y"}, id="a-worker-credential"),
        pytest.param({"access_token": "alk_pat_abc"}, id="a-personal-access-token"),
    ],
)
def test_anything_but_a_machine_credential_is_refused_and_nothing_is_kept(
    home: Path, token: dict[str, Any]
) -> None:
    backend = Backend(token=token)
    with pytest.raises(PersonalBoxError, match="machine credential"):
        _register(backend)
    assert [path for path, _ in backend.seen if path.endswith("/claim")] == []
    assert not (home / "personal_box.json").exists()
    assert not (home / "auth.yml").exists()


def test_a_refused_claim_keeps_nothing(home: Path) -> None:
    with pytest.raises(PersonalBoxError, match="claim"):
        _register(Backend(claim_status=404))
    assert not (home / "personal_box.json").exists()


def test_a_denied_approval_keeps_nothing(home: Path) -> None:
    with pytest.raises(device_flow.AuthorizationDeniedError):
        _register(Backend(token={"error": "access_denied"}))
    assert not (home / "personal_box.json").exists()


# --------------------------------------------------------------------------- #
# the record
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("{not json", id="corrupt"),
        pytest.param(json.dumps({"schema_version": "1.0.0", "api_url": API}), id="no-credential"),
        pytest.param(
            json.dumps({"schema_version": "1.0.0", "credential": CREDENTIAL, "api_url": API}),
            id="no-pod",
        ),
    ],
)
def test_a_record_that_cannot_run_a_box_is_no_record(home: Path, body: str) -> None:
    (home / "personal_box.json").write_text(body, encoding="utf-8")
    assert load_personal_box() is None


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("v*.json")), ids=lambda p: p.stem)
def test_every_historical_record_still_reads(home: Path, fixture: Path) -> None:
    (home / "personal_box.json").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    record = load_personal_box()
    assert record is not None and record.credential.startswith("alk_machine_")


# --------------------------------------------------------------------------- #
# the commands
# --------------------------------------------------------------------------- #


def _scripted(backend: Backend) -> Callable[..., PersonalBoxRecord]:
    real = personal_box.register_personal_box
    transport = httpx.MockTransport(backend)

    def run(api_url: str, **kwargs: Any) -> PersonalBoxRecord:
        return real(
            api_url, transport=transport, claim_transport=transport, sleep=lambda _s: None, **kwargs
        )

    return run


def test_the_register_command_shows_where_to_approve_and_keeps_no_login(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(personal_box, "register_personal_box", _scripted(Backend()))
    result = CliRunner().invoke(
        app, ["cloud-mirror", "register", "--api-url", API, "--machine-name", "my laptop"]
    )
    assert result.exit_code == 0, result.output
    assert "ABCD-EFGH" in result.output
    assert f"{API}/device?user_code=ABCD-EFGH" in result.output
    assert load_personal_box() is not None
    assert not (home / "auth.yml").exists()


def test_the_register_command_refuses_to_overwrite_without_force(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(personal_box, "register_personal_box", _scripted(Backend()))
    first = CliRunner().invoke(app, ["cloud-mirror", "register", "--api-url", API])
    assert first.exit_code == 0, first.output
    again = CliRunner().invoke(app, ["cloud-mirror", "register", "--api-url", API])
    assert again.exit_code == 1
    assert "--force" in again.output
    forced = CliRunner().invoke(app, ["cloud-mirror", "register", "--api-url", API, "--force"])
    assert forced.exit_code == 0, forced.output


def test_the_register_command_says_why_it_failed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(personal_box, "register_personal_box", _scripted(Backend(claim_status=404)))
    result = CliRunner().invoke(app, ["cloud-mirror", "register", "--api-url", API])
    assert result.exit_code == 1
    assert "claim" in result.output
    assert load_personal_box() is None


def test_run_serves_on_the_record_and_reads_no_login(home: Path, tmp_path: Path) -> None:
    _register(Backend())
    settings = mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name="lap")
    record = load_personal_box()
    assert record is not None
    assert settings.machine_credential == record.credential
    assert settings.token == record.credential
    assert settings.provider_pod_id == record.provider_pod_id
    assert settings.api_url == API
    assert settings.user_id == ""
    assert not (home / "auth.yml").exists()


def test_a_platform_credential_in_the_environment_wins_over_the_record(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(Backend())
    monkeypatch.setenv("ALKERA_MACHINE_CREDENTIAL", "alk_machine_" + "p" * 64)
    monkeypatch.setenv("ALKERA_MACHINE_PROVIDER_POD_ID", "pod-env")
    monkeypatch.setenv("ALKERA_API_URL", "http://env.test")
    settings = mirror_settings_from_auth(api_url=None, project_dir=tmp_path, machine_name="lap")
    assert settings.machine_credential == "alk_machine_" + "p" * 64
    assert settings.provider_pod_id == "pod-env"
