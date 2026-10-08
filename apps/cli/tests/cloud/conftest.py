"""Fixtures for the cloud-mirror suite: the backend's own server + org
fixtures, imported so the daemon's transport is exercised against the REAL
realtime gateway (a uvicorn instance on the test loop, real Postgres) rather
than a mock of it. Importing a fixture function registers it for this
directory, autouse ones included.

A chat must be DECLARED before the gateway lets anyone subscribe to it — in
production by the chat's workspace object, here through the backend suite's
declaration seam (``tests.chat_declarations``): a test declares the chat it
mirrors, and an undeclared id is what the gateway refuses."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from alkera_cli.cloud.rest import CloudRestClient
from tests.chat_declarations import declared_chats  # noqa: F401  (autouse fixture)
from tests.conftest import (  # noqa: F401  (fixtures registered by import)
    _captcha_off_by_default,
    _gateway_catalog_by_default,
    _reset_rate_limiters,
    _reset_realtime_state,
    client,
    org_admin,
    real_session,
    uvicorn_server,
)
from tests.test_ws_gateway import (  # noqa: F401
    _fast_ticks,
    _reset_revocation_cache,
)


@pytest.fixture(autouse=True)
def _a_box_here_never_pre_warms_the_real_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Point this suite's Alkera home at a throwaway and gate the pre-warm off.

    ``alkera_cli.host.paths.ALKERA_HOME`` defaults to ``~/.alkera`` and the default
    suite sets no override, so ``paths.cache_dir()`` (where the agent binary is
    staged) and ``paths.agents_dir()`` (the spawned-agent registry the orphan
    sweep reaps from) are ONE directory shared by every xdist worker on the box.

    Starting a box — ``CloudMirrorService.start()``, which most modules in this
    directory do — fires ``start_prewarm_in_background(force=True)``. That
    stages a multi-hundred-MB binary into that shared cache and — wherever one
    resolves — spawns a real opencode whose first-run database migration takes
    no cross-process lock, so a suite run sixteen-wide stages into, spawns
    into, and reaps from a directory fifteen other workers are using. And it
    is a module-global task nobody awaits: the resolver's worker thread and
    any subprocess transport it opened are still live when pytest closes the
    loop the test ran on, which is the shape a worker dies in on Windows with
    no Python traceback to attribute it by.

    No test in this directory asserts the pre-warm: it is a start-up
    accelerator, not behaviour this suite covers. The env token gates it off
    (it beats the caller's ``force``), and the home is repointed so anything
    else that reaches for a per-user path lands in this test's tmp dir. The one
    test that IS about the pre-warm opts back in explicitly.
    """
    from alkera_cli.harness import prewarm
    from alkera_cli.host import paths

    # Off ``tmp_path``, not inside it: a test that asserts its own workspace is
    # empty must not find an Alkera home sitting in it.
    home = tmp_path_factory.mktemp("alkera-home")
    monkeypatch.setenv("ALKERA_HOME", str(home))
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setenv("ALKERA_HARNESS_PREWARM", "0")
    prewarm._reset_for_tests()
    yield
    prewarm._reset_for_tests()


@pytest.fixture(autouse=True)
def _chat_gateway_token_minted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every session open mints the chat's gateway token from the backend, and
    a refused mint opens no session. The tests here drive the mirror through
    scripted transports or the realtime gateway, none of which is about that
    credential, so the mint answers with a fixed token. The module that pins
    the mint itself overrides this fixture by name and meets the real call."""

    async def _mint(self: CloudRestClient, chat_id: str) -> dict[str, Any]:
        return {"token": f"gw-token-{chat_id}", "expires_at": None}

    monkeypatch.setattr(CloudRestClient, "mint_gateway_token", _mint)


@pytest.fixture(autouse=True)
def _turn_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before every turn the box asks the backend whether the message's author
    may still send, as the chat's publisher. The rigs here run a box that is
    not a registered publisher (the same reason the gateway mint is answered
    above), and none of them is about that check, so it admits. The module
    that pins the check overrides this fixture by name and meets the real
    call through its scripted transport."""

    async def _admitted(self: CloudRestClient, chat_id: str, *, user_id: str) -> dict[str, Any]:
        return {"allowed": True, "code": None, "message": None}

    monkeypatch.setattr(CloudRestClient, "send_admission", _admitted)


@pytest.fixture(autouse=True)
def _no_stop_in_this_suite_arms_the_real_process_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every stop arms a timer that ends the PROCESS once the drain ceiling and
    its margin have passed. Here that process is the xdist worker, and a test
    that drains with a short ceiling would kill it minutes later inside some
    other test. A test asserting on the arming passes its own ``hard_stop``."""

    def _never(seconds: float, action: Any) -> None:
        del seconds, action

    monkeypatch.setattr("alkera_cli.cloud.service.arm_hard_stop", _never)


@pytest.fixture(autouse=True)
def _the_hosts_memory_is_not_read_as_pressure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A box's memory pressure valve reads the machine it runs on. Here that is
    the test runner, whose memory says nothing about the chats a test serves
    and would sleep them at random under load. A test about the valve passes
    its own ``memory``."""
    monkeypatch.setattr("alkera_cli.cloud.service.chats_memory", lambda: None)
