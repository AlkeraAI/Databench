"""The crash hook is absent unless a local box asked for it.

The hook kills the process. Nothing about that is safe anywhere but a
developer's own machine, so the interesting assertions here are the NEGATIVE
ones: every environment that is not "local, and opted in" builds a Files router
with no ``/_test/crash`` path in it at all — not a 403, not a 404 handler, no
path — and the killer is never reached.
"""

from __future__ import annotations

import os
import signal
import uuid

import httpx
import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.config import settings
from alkera_core.db.session import get_db
from alkera_core.files.errors import KNOWN_CODES
from backend.api.deps.files_errors import register_files_error_handlers
from backend.api.routes.files import build_files_router
from backend.api.routes.files._testhooks import (
    CRASH_POINTS,
    TEST_HOOKS_ENV,
    _sigkill_self,
    build_test_hooks_router,
    hooks_enabled,
)
from backend.auth.dependencies import current_principal
from fastapi import FastAPI

OPEN_ENV = {TEST_HOOKS_ENV: "1"}

# The async cases below carry no `anyio` marker on purpose. The suite runs
# `asyncio_mode = "auto"` with a SESSION-scoped loop, and the autouse async
# fixtures every backend test gets — the rate-limit window reset among them —
# open asyncpg connections on that loop. `pytest.mark.anyio` instead runs the
# test body on a fresh loop it closes when the test ends, which strands those
# connections and fails their teardown with "Event loop is closed". Auto mode
# puts body and fixtures on the one loop, which is what the rest of the
# directory does.


@pytest.mark.parametrize(
    ("app_env", "environ", "expected"),
    [
        pytest.param("local", {TEST_HOOKS_ENV: "1"}, True, id="local-and-opted-in"),
        pytest.param("local", {}, False, id="local-but-never-opted-in"),
        pytest.param("local", {TEST_HOOKS_ENV: "0"}, False, id="local-but-opted-out"),
        pytest.param("local", {TEST_HOOKS_ENV: "true"}, False, id="local-but-not-the-one-value"),
        pytest.param("staging", {TEST_HOOKS_ENV: "1"}, False, id="staging-even-opted-in"),
        pytest.param("production", {TEST_HOOKS_ENV: "1"}, False, id="production-even-opted-in"),
    ],
)
def test_gate_needs_local_and_the_flag(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
    environ: dict[str, str],
    expected: bool,
) -> None:
    monkeypatch.setattr(settings, "app_env", app_env)
    assert hooks_enabled(environ) is expected


@pytest.mark.parametrize(
    ("app_env", "environ"),
    [
        pytest.param("local", {}, id="local-but-never-opted-in"),
        pytest.param("production", OPEN_ENV, id="production-even-opted-in"),
        pytest.param("staging", OPEN_ENV, id="staging-even-opted-in"),
    ],
)
def test_no_router_at_all_when_the_gate_is_shut(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
    environ: dict[str, str],
) -> None:
    monkeypatch.setattr(settings, "app_env", app_env)
    assert build_test_hooks_router(environ=environ) is None


def test_router_exists_when_the_gate_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "local")
    router = build_test_hooks_router(environ=OPEN_ENV)
    assert router is not None
    assert [route.path for route in router.routes] == ["/_test/crash"]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("app_env", "flag"),
    [
        pytest.param("local", None, id="local-but-never-opted-in"),
        pytest.param("production", "1", id="production-even-opted-in"),
    ],
)
def test_files_router_carries_no_crash_path_when_shut(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
    flag: str | None,
) -> None:
    """The include line in the package barrel obeys the same gate."""
    monkeypatch.setattr(settings, "app_env", app_env)
    if flag is None:
        monkeypatch.delenv(TEST_HOOKS_ENV, raising=False)
    else:
        monkeypatch.setenv(TEST_HOOKS_ENV, flag)
    paths = [route.path for route in build_files_router().routes]  # type: ignore[attr-defined]
    assert not [path for path in paths if "_test" in path]


def test_files_router_carries_the_crash_path_when_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setenv(TEST_HOOKS_ENV, "1")
    paths = [route.path for route in build_files_router().routes]  # type: ignore[attr-defined]
    assert "/api/v1/files/_test/crash" in paths


async def _post_crash(router_app: FastAPI, at: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=router_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post("/_test/crash", params={"at": at})


def _signed_in() -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(uuid.uuid4()),
            org_id=uuid.uuid4(),
            credential=CredentialKind.JWT,
        )
    )


def _app_with_hook(killer: object, *, authenticated: bool = True) -> FastAPI:
    """The hook on a bare app, with the family's error handler and its principal.

    ``authenticated`` swaps the credential resolution for a signed-in one, the
    way every other Files route test does; leaving it off exercises the real
    dependency against a request that carries nothing.
    """
    app = FastAPI()
    router = build_test_hooks_router(killer=killer, environ=OPEN_ENV)  # type: ignore[arg-type]
    assert router is not None
    app.include_router(router)
    register_files_error_handlers(app)
    if authenticated:
        app.dependency_overrides[current_principal] = _signed_in
    else:
        app.dependency_overrides[get_db] = lambda: None
    return app


@pytest.mark.parametrize("at", sorted(CRASH_POINTS))
async def test_a_known_point_kills_the_process(
    monkeypatch: pytest.MonkeyPatch,
    at: str,
) -> None:
    monkeypatch.setattr(settings, "app_env", "local")
    killed: list[int] = []
    response = await _post_crash(_app_with_hook(lambda: killed.append(1)), at)
    assert response.status_code == 204
    assert killed == [1]


@pytest.mark.parametrize(
    "at",
    [
        pytest.param("content.before_comit", id="typo"),
        pytest.param("", id="empty"),
        pytest.param("content.*", id="wildcard"),
        pytest.param("upload.before_commit", id="a-point-the-library-does-not-have"),
    ],
)
async def test_an_unknown_point_refuses_and_kills_nothing(
    monkeypatch: pytest.MonkeyPatch,
    at: str,
) -> None:
    monkeypatch.setattr(settings, "app_env", "local")
    killed: list[int] = []
    response = await _post_crash(_app_with_hook(lambda: killed.append(1)), at)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "files.unknown_crash_point"
    assert body["code"] in KNOWN_CODES, "the refusal must be a code the catalogue knows"
    assert killed == []


@pytest.mark.parametrize("at", sorted(CRASH_POINTS))
async def test_an_unauthenticated_caller_cannot_kill_the_backend(
    monkeypatch: pytest.MonkeyPatch,
    at: str,
) -> None:
    """The hook resolves a credential before it acts, like every Files route.

    An opted-in developer box still runs a browser: without this, any page that
    could reach the port could POST here and take the backend down.
    """
    monkeypatch.setattr(settings, "app_env", "local")
    killed: list[int] = []
    response = await _post_crash(_app_with_hook(lambda: killed.append(1), authenticated=False), at)
    assert response.status_code == 401
    assert killed == [], "the process was killed for a caller that never signed in"


#: The POSIX rows name their signal rather than referencing it, and skip where
#: it does not exist. `signal.SIGKILL` evaluated in a decorator is itself the
#: bug this module is about: on Windows it raises at COLLECTION, which takes
#: down the worker before a single case in the file has run.
_NO_SIGKILL = pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"), reason="this platform's signal module has no SIGKILL"
)


@pytest.mark.parametrize(
    ("platform", "expected_signal"),
    [
        pytest.param("linux", "SIGKILL", id="linux-sigkill", marks=_NO_SIGKILL),
        pytest.param("darwin", "SIGKILL", id="darwin-sigkill", marks=_NO_SIGKILL),
        pytest.param("win32", "SIGTERM", id="windows-terminateprocess"),
    ],
)
def test_the_default_killer_picks_the_signal_its_platform_actually_has(
    platform: str,
    expected_signal: str,
) -> None:
    """``signal.SIGKILL`` does not exist on Windows.

    Naming it unconditionally would not make the hook merely wrong there — the
    attribute lookup raises before ``os.kill`` is called, so the process the
    proof asked to die keeps serving and the proof waits for a crash that never
    comes. On Windows every signal other than the two console events routes to
    ``TerminateProcess``, which ends the process with no handler and no unwind:
    the property the proof is about.
    """
    sent: list[tuple[int, int]] = []
    _sigkill_self(platform=platform, kill=lambda pid, sig: sent.append((pid, sig)))
    assert sent == [(os.getpid(), getattr(signal, expected_signal))]


def test_the_windows_killer_never_looks_up_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the Windows ``signal`` module: no ``SIGKILL`` on it at all."""
    monkeypatch.delattr(signal, "SIGKILL", raising=False)
    sent: list[tuple[int, int]] = []

    _sigkill_self(platform="win32", kill=lambda pid, sig: sent.append((pid, sig)))

    assert sent == [(os.getpid(), signal.SIGTERM)]
