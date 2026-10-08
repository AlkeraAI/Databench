"""A platform box claims the machine its credential names, and says its load.

Three things separate a platform box from an org's own box, and each is a test
here:

* **How it registers.** With ``ALKERA_MACHINE_CREDENTIAL`` set the box calls
  ``POST /api/v1/machines/claim`` with the credential in its header and the
  catalog code left out entirely — the credential says what the box is. Without
  it the box registers the old way. A box holding BOTH a credential and a
  box-user session claims: what the platform says a box is outranks the grant
  its operator's account happens to carry.
* **What it says on every beat.** Capacity, chats served and the daemon build.
  That is how the backend spreads a shared pool: placement puts a new chat on
  the ready box with the most room, which it can only know because the box
  says so.
* **What it does when the credential goes away.** An admin revoking a
  credential is not an outage to retry through: the box gives up, which is what
  makes ``stop`` put every chat it holds to sleep — folder pushed, lease
  released — so the chats land on another box.

The REST client is scripted rather than mocked-through: what the box SENDS is
the contract under test, so each fake records the call and answers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud.activity import ChatActivity
from alkera_cli.cloud.publisher_identity import ENV_MACHINE_CREDENTIAL, MachineIdentity
from alkera_cli.cloud.rest import CloudApiError, CloudRestClient
from alkera_cli.cloud.service import (
    CloudMirrorService,
    MirrorSettings,
    box_rest_client,
    default_max_mirrors,
)
from alkera_cli.cloud.transport import CloudSocket
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.auth.machine_token import (
    MACHINE_CREDENTIAL_HEADER,
    MACHINE_CREDENTIAL_REFUSED,
    MACHINE_CREDENTIAL_REQUIRED,
    MACHINE_TOKEN_PREFIX,
)
from alkera_core.authz.headers import ACTOR_HEADER, AGENT_ID_HEADER
from alkera_core.project.directory import ProjectDirectory

CREDENTIAL = MACHINE_TOKEN_PREFIX + "pool-box-secret"
HEARTBEAT = 20.0


class _LoopStoppedError(Exception):
    """Ends the machine loop from inside its own sleep seam."""


class _NoSocket(CloudSocket):
    """A socket that never connects: the machine loop is what is under test."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def rebind(self, rest: CloudRestClient) -> None:
        return None


class _RecordingRest(CloudRestClient):
    """Records every machine call the box makes and answers from a script."""

    def __init__(
        self,
        *,
        claim_error: tuple[int, str] | None = None,
        heartbeat_error: tuple[int, str] | None = None,
        claim_answer: dict[str, Any] | None = None,
        beat_answers: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self.claims: list[dict[str, Any]] = []
        self.registrations: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self._claim_error = claim_error
        self._heartbeat_error = heartbeat_error
        self._claim_answer = claim_answer or {}
        self._beat_answers = list(beat_answers or [])

    async def claim_machine(self, **kwargs: Any) -> dict[str, Any]:
        self.claims.append(dict(kwargs))
        if self._claim_error is not None:
            status, code = self._claim_error
            raise CloudApiError(
                status,
                {"error": {"code": code, "message": "refused"}},
                method="POST",
                path="/api/v1/machines/claim",
            )
        return {"id": "machine-pool-1", **self._claim_answer}

    def for_agent(self, agent_id: str) -> CloudRestClient:
        """The service re-clones its client as the machine once it registers;
        the clone has to keep recording or the beats under test would go to the
        real one."""
        return self

    async def register_machine(self, **kwargs: Any) -> dict[str, Any]:
        self.registrations.append(dict(kwargs))
        return {"id": "machine-org-1"}

    async def heartbeat_machine(self, machine_id: str, **kwargs: Any) -> dict[str, Any]:
        self.heartbeats.append({"machine_id": machine_id, **kwargs})
        if self._heartbeat_error is not None:
            status, code = self._heartbeat_error
            raise CloudApiError(
                status,
                {"error": {"code": code, "message": "refused"}},
                method="POST",
                path=f"/api/v1/machines/{machine_id}/heartbeat",
            )
        return self._beat_answers.pop(0) if self._beat_answers else {}


class _Idle:
    """A served chat with nothing in flight: all a beat reads off a mirror."""

    activity = ChatActivity.IDLE


class _Working:
    activity = ChatActivity.WORKING


def _service(
    tmp_path: Path,
    rest: _RecordingRest,
    *,
    credential: str = CREDENTIAL,
    max_mirrors: int = 4,
    stop_after: int = 3,
) -> CloudMirrorService:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= stop_after:
            raise _LoopStoppedError

    settings = MirrorSettings(
        api_url="http://127.0.0.1:1",
        token="t",
        project_dir=tmp_path,
        machine_name="pool box",
        provider_pod_id="i-0abc",
        machine_type_code="m6i.xlarge",
        machine_credential=credential,
        daemon_version="9.9.9",
        heartbeat_interval=HEARTBEAT,
        max_mirrors=max_mirrors,
    )
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    return CloudMirrorService(settings, runtime, rest=rest, socket=_NoSocket(rest), sleep=sleep)


async def _run_loop(service: CloudMirrorService) -> None:
    with pytest.raises(_LoopStoppedError):
        await service._machine_loop()


# ---- which route the box takes -------------------------------------------


async def test_a_box_with_a_credential_claims_instead_of_registering(tmp_path: Path) -> None:
    rest = _RecordingRest()
    service = _service(tmp_path, rest)

    assert await service._register() is True

    assert rest.registrations == []
    assert len(rest.claims) == 1
    claim = rest.claims[0]
    assert claim["credential"] == CREDENTIAL
    assert claim["provider_pod_id"] == "i-0abc"
    assert claim["name"] == "pool box"
    # The credential names the kind and the size; the box never gets to.
    assert "machine_type_code" not in claim
    assert claim["capacity"] == 4
    assert claim["daemon_version"] == "9.9.9"


async def test_a_box_without_a_credential_still_registers_as_its_orgs_own(tmp_path: Path) -> None:
    rest = _RecordingRest()
    service = _service(tmp_path, rest, credential="")

    assert await service._register() is True

    assert rest.claims == []
    instance = rest.registrations[0].pop("daemon_instance_id")
    assert isinstance(instance, str) and instance
    assert rest.registrations == [
        {
            "name": "pool box",
            "provider": "self_hosted",
            "provider_pod_id": "i-0abc",
            "machine_type_code": "m6i.xlarge",
        }
    ]


async def test_the_claim_binds_the_box_to_the_machine_the_backend_named(tmp_path: Path) -> None:
    """What a claim is FOR: the box publishes as the machine id it comes back
    with, so the very first transcript write asserts the id the chat row names."""
    rest = _RecordingRest()
    service = _service(tmp_path, rest)

    await service._register()

    assert service._machine_id == "machine-pool-1"


# ---- what the wire sees: the assertion follows the bearer -----------------


class _Wire:
    """Answers the machine routes and keeps every request it saw."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/machines/claim":
            return httpx.Response(200, json={"id": "machine-pool-1"})
        if path == "/api/v1/machines/register":
            return httpx.Response(201, json={"id": "machine-org-1"})
        if path == "/api/v1/chats":
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        return httpx.Response(200, json={})

    def sent(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


def _settings_on(tmp_path: Path, *, token: str, credential: str) -> MirrorSettings:
    return MirrorSettings(
        api_url="http://127.0.0.1:1",
        token=token,
        project_dir=tmp_path,
        machine_name="pool box",
        provider_pod_id="i-0abc",
        machine_type_code="m6i.xlarge",
        machine_credential=credential,
        daemon_version="9.9.9",
        heartbeat_interval=HEARTBEAT,
    )


async def _one_pass(tmp_path: Path, settings: MirrorSettings) -> _Wire:
    """The box's first registration and the beat after it, on a watched wire —
    through the client the service builds for itself."""
    wire = _Wire()
    rest = box_rest_client(settings, transport=httpx.MockTransport(wire))

    async def sleep(seconds: float) -> None:
        raise _LoopStoppedError

    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    service = CloudMirrorService(settings, runtime, rest=rest, socket=_NoSocket(rest), sleep=sleep)
    await _run_loop(service)
    return wire


async def test_a_box_on_its_credential_asserts_no_agent_until_it_is_the_machine(
    tmp_path: Path,
) -> None:
    """The claim is the credential as the bearer and in its own header, and no
    agent assertion beside them — the backend admits an assertion on this
    bearer only when it names the box's own machine, which is what the claim
    is about to tell it — and the first beat after it asserts the machine id
    the claim answered."""
    credential = MACHINE_TOKEN_PREFIX + "x" * 48
    wire = await _one_pass(
        tmp_path, _settings_on(tmp_path, token=credential, credential=credential)
    )

    (claim,) = wire.sent("/api/v1/machines/claim")
    assert claim.headers["Authorization"] == f"Bearer {credential}"
    assert claim.headers[MACHINE_CREDENTIAL_HEADER] == credential
    assert ACTOR_HEADER not in claim.headers
    assert AGENT_ID_HEADER not in claim.headers
    (beat,) = wire.sent("/api/v1/machines/machine-pool-1/heartbeat")
    assert beat.headers["Authorization"] == f"Bearer {credential}"
    assert beat.headers[AGENT_ID_HEADER] == "machine-pool-1"


async def test_an_org_box_asserts_the_machine_it_registers_as(tmp_path: Path) -> None:
    wire = await _one_pass(tmp_path, _settings_on(tmp_path, token="device-jwt", credential=""))

    (register,) = wire.sent("/api/v1/machines/register")
    assert register.headers["Authorization"] == "Bearer device-jwt"
    assert register.headers[AGENT_ID_HEADER] == "machine:pool-box"
    assert MACHINE_CREDENTIAL_HEADER not in register.headers
    (beat,) = wire.sent("/api/v1/machines/machine-org-1/heartbeat")
    assert beat.headers[AGENT_ID_HEADER] == "machine-org-1"


# ---- the credential rides only the call that needs it ---------------------


def test_the_credential_is_sent_as_a_header_on_the_claim_and_nowhere_else() -> None:
    """A secret one route wants must not ride every other request the box makes
    — the transcript writes, the file leases, the event stream all carry the
    box-user session and nothing more."""
    client = CloudRestClient(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")

    assert MACHINE_CREDENTIAL_HEADER not in client.headers()
    assert all(MACHINE_TOKEN_PREFIX not in value for value in client.headers().values())


# ---- what a beat says -----------------------------------------------------


async def test_every_heartbeat_carries_the_load_the_pool_is_spread_by(tmp_path: Path) -> None:
    rest = _RecordingRest()
    service = _service(tmp_path, rest, max_mirrors=5)
    await service._register()
    service._mirrors = {"chat-a": _Idle(), "chat-b": _Idle()}  # type: ignore[dict-item]

    await _run_loop(service)

    assert rest.heartbeats
    for beat in rest.heartbeats:
        assert beat["machine_id"] == "machine-pool-1"
        assert beat["capacity"] == 5
        assert beat["chats_served"] == 2
        assert beat["daemon_version"] == "9.9.9"
        assert beat["credential"] == CREDENTIAL


async def test_an_org_box_beats_without_a_credential(tmp_path: Path) -> None:
    rest = _RecordingRest()
    service = _service(tmp_path, rest, credential="")
    await service._register()

    await _run_loop(service)

    assert rest.heartbeats
    assert all(beat["credential"] == "" for beat in rest.heartbeats)


# ---- which process is speaking ---------------------------------------------


@pytest.mark.parametrize("credential", [CREDENTIAL, ""], ids=["claim", "register"])
async def test_every_beat_names_the_process_that_registered(
    tmp_path: Path, credential: str
) -> None:
    """The platform takes a beat only from the process that registered last,
    so the id the box registers with and the id every beat carries are one
    id, and a second process on the same box has its own."""
    rest = _RecordingRest()
    service = _service(tmp_path, rest, credential=credential)
    await service._register()
    await _run_loop(service)
    registered = (rest.claims or rest.registrations)[0]["daemon_instance_id"]
    assert registered
    assert rest.heartbeats
    assert {beat["daemon_instance_id"] for beat in rest.heartbeats} == {registered}

    successor_rest = _RecordingRest()
    successor = _service(tmp_path / "next", successor_rest, credential=credential)
    await successor._register()
    assert (successor_rest.claims or successor_rest.registrations)[0][
        "daemon_instance_id"
    ] != registered


async def test_a_process_told_it_is_stale_stops_beating_and_does_not_re_register(
    tmp_path: Path,
) -> None:
    """The exiting process of a restart: re-registering would take the row back
    from the process that is serving, and beating on is only refused."""
    rest = _RecordingRest(heartbeat_error=(409, "machine_stale_instance"))
    service = _service(tmp_path, rest, credential="", stop_after=4)
    await service._register()

    await _run_loop(service)

    assert len(rest.heartbeats) == 1
    assert len(rest.registrations) == 1
    assert service.machine_id == "machine-org-1"


# ---- a credential the platform took away ----------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        pytest.param(401, MACHINE_CREDENTIAL_REFUSED, id="revoked-or-unknown"),
        pytest.param(401, MACHINE_CREDENTIAL_REQUIRED, id="not-presented"),
    ],
)
async def test_a_refused_claim_stops_the_box_instead_of_being_retried(
    tmp_path: Path, status: int, code: str
) -> None:
    rest = _RecordingRest(claim_error=(status, code))
    service = _service(tmp_path, rest)

    assert await service._register() is False

    assert service.gave_up.is_set()
    assert service.failure is not None
    assert code in service.failure
    assert service._machine_id is None


async def test_a_box_that_gave_up_asks_nothing_more(tmp_path: Path) -> None:
    """Re-asking cannot change a revoked credential, and every ask puts another
    refusal on the platform's audit trail on the way out."""
    rest = _RecordingRest(claim_error=(401, MACHINE_CREDENTIAL_REFUSED))
    service = _service(tmp_path, rest)

    await service._machine_loop()

    assert len(rest.claims) == 1


async def test_a_credential_revoked_while_the_box_serves_stops_it_on_the_next_beat(
    tmp_path: Path,
) -> None:
    rest = _RecordingRest(heartbeat_error=(401, MACHINE_CREDENTIAL_REFUSED))
    service = _service(tmp_path, rest)
    await service._register()

    await service._machine_loop()

    assert service.gave_up.is_set()
    assert service.failure is not None
    assert MACHINE_CREDENTIAL_REFUSED in service.failure
    # One beat, then the loop stops: it does not keep beating for a box the
    # platform has taken away.
    assert len(rest.heartbeats) == 1


async def test_an_ordinary_heartbeat_failure_is_not_a_reason_to_stop(tmp_path: Path) -> None:
    """A 500 or a flaky tunnel is an outage the box rides out — only the
    credential verdict ends it."""
    rest = _RecordingRest(heartbeat_error=(500, ""))
    service = _service(tmp_path, rest)
    await service._register()

    await _run_loop(service)

    assert not service.gave_up.is_set()
    assert len(rest.heartbeats) > 1


# ---- reading the credential off the box's environment ---------------------


def test_the_credential_comes_from_the_environment_provisioning_writes() -> None:
    identity = MachineIdentity.from_env(
        name="pool box",
        env={
            "ALKERA_MACHINE_PROVIDER_POD_ID": "i-0abc",
            ENV_MACHINE_CREDENTIAL: CREDENTIAL,
        },
    )

    assert identity.credential == CREDENTIAL
    assert identity.is_platform
    # No catalog code needed: the credential carries the kind and the size.
    assert identity.missing() == []


def test_a_box_with_neither_a_credential_nor_a_catalog_code_refuses_to_start() -> None:
    identity = MachineIdentity.from_env(
        name="pool box", env={"ALKERA_MACHINE_PROVIDER_POD_ID": "i-0abc"}
    )

    assert not identity.is_platform
    assert any("ALKERA_MACHINE_TYPE_CODE" in gap for gap in identity.missing())


def test_a_platform_box_still_has_to_say_which_instance_it_is() -> None:
    identity = MachineIdentity.from_env(name="pool box", env={ENV_MACHINE_CREDENTIAL: CREDENTIAL})

    assert any("ALKERA_MACHINE_PROVIDER_POD_ID" in gap for gap in identity.missing())


def test_the_credential_never_reaches_a_log_line_through_the_identitys_repr() -> None:
    identity = MachineIdentity(name="pool box", provider_pod_id="i-0abc", credential=CREDENTIAL)

    assert CREDENTIAL not in repr(identity)
    assert "i-0abc" in repr(identity)


def test_the_credential_never_reaches_a_log_line_through_the_settings_repr(tmp_path: Path) -> None:
    settings = MirrorSettings(
        api_url="http://127.0.0.1:1",
        token="t",
        project_dir=tmp_path,
        machine_name="pool box",
        machine_credential=CREDENTIAL,
    )

    assert CREDENTIAL not in repr(settings)


# ---- how many chats a box of this size holds ------------------------------


@pytest.mark.parametrize(
    ("cores", "expected"),
    [
        pytest.param(1, 2, id="one-core-still-holds-two"),
        pytest.param(2, 2, id="two-cores"),
        pytest.param(4, 4, id="four-cores"),
        pytest.param(64, 12, id="a-huge-box-is-still-capped"),
        pytest.param(0, 6, id="a-host-that-cannot-count-keeps-the-default"),
    ],
)
def test_capacity_is_read_off_the_hardware_between_a_floor_and_a_ceiling(
    cores: int, expected: int
) -> None:
    assert default_max_mirrors(cores) == expected


# ---- what a beat says about the machine, and what the box is told -------------


def _card(name: str) -> dict[str, Any]:
    return {
        "card": {
            "name": name,
            "gpu": {"name": "A40", "count": 1, "memory_gb": 48},
            "vcpu": 9,
            "memory_gb": 50,
            "disk_gb": 100,
            "billed_per_minute": True,
            "idle_stop_minutes": 30,
        }
    }


async def test_the_claim_answer_card_is_what_the_box_holds(tmp_path: Path) -> None:
    rest = _RecordingRest(claim_answer=_card("Trainer"))
    service = _service(tmp_path, rest)
    await service._register()
    card = service._pulse.machine_card.card
    assert card is not None and card.name == "Trainer"


async def test_a_beat_answer_renames_the_machine_the_box_holds(tmp_path: Path) -> None:
    rest = _RecordingRest(claim_answer=_card("Trainer"), beat_answers=[_card("Renamed")])
    service = _service(tmp_path, rest, stop_after=1)
    await service._register()
    before = service._pulse.machine_card.revision
    await _run_loop(service)
    card = service._pulse.machine_card.card
    assert card is not None and card.name == "Renamed"
    assert service._pulse.machine_card.revision == before + 1


async def test_a_beat_sends_no_activity_until_a_chat_works_then_the_moment(
    tmp_path: Path,
) -> None:
    rest = _RecordingRest()
    service = _service(tmp_path, rest, stop_after=2)
    await service._register()
    service._mirrors = {"chat-a": _Idle()}  # type: ignore[dict-item]
    await _run_loop(service)
    assert rest.heartbeats and all(b["last_activity_at"] is None for b in rest.heartbeats)

    rest.heartbeats.clear()
    service._mirrors = {"chat-a": _Idle(), "chat-b": _Working()}  # type: ignore[dict-item]
    with pytest.raises(_LoopStoppedError):
        await service._machine_loop()
    stamps = [b["last_activity_at"] for b in rest.heartbeats]
    assert stamps and all(stamp is not None for stamp in stamps)


# ---- the two heartbeats claim the same chat-serving capabilities ----------


async def test_a_supervised_box_claims_everything_the_single_daemon_serves(
    tmp_path: Path,
) -> None:
    """The supervisor's workers run the same chat mirror as the single daemon,
    so its beat carries every capability the daemon's does that is not about
    the hardware (the supervisor takes no GPU sample). A supervised box that
    left out ``folder_flush_v1`` was never asked to flush a moving workspace."""
    import json

    from alkera_cli.box_capabilities import CLAIMS, ClaimedBy
    from alkera_cli.supervisor.service import FileRoutingFeed, Supervisor
    from alkera_cli.supervisor.slots import SlotTable
    from alkera_core.compute.box_isolation import IsolationReport

    wire = await _one_pass(tmp_path / "single", _settings_on(tmp_path, token="t", credential=""))
    (beat,) = wire.sent("/api/v1/machines/machine-org-1/heartbeat")
    single = set(json.loads(beat.content)["capabilities"])
    supervisor = Supervisor(
        api=None,  # type: ignore[arg-type]  # only the body is built
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        # A probe that proved nothing: org_isolation is the probe's to claim.
        isolation=IsolationReport(frozenset()),
        slots=SlotTable(tmp_path / "slots.json"),
        env={},
    )
    supervised = set(supervisor.heartbeat_body()["capabilities"])  # type: ignore[call-overload]

    hardware = {c.value for c, part in CLAIMS.items() if part is ClaimedBy.HARDWARE}
    assert single - hardware <= supervised
    assert "folder_flush_v1" in supervised
    # The supervisor runs the workers and grows the volume under them.
    assert supervised - single == {"org_workers", "disk_grow_online"}


def test_every_capability_names_what_claims_it() -> None:
    from alkera_cli.box_capabilities import CLAIMS, ClaimedBy
    from alkera_core.compute.box_contract import BoxCapability

    assert set(CLAIMS) == set(BoxCapability)
    assert CLAIMS[BoxCapability.ORG_ISOLATION] is ClaimedBy.PROBE
