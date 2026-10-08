"""An org worker serves its org only: rows naming another org (or none) and
chats the supervisor did not route are refused, whatever else says so."""

from __future__ import annotations

import asyncio
import re
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from _mirror_service import Clock, build_service
from alkera_cli.cloud import org_worker
from alkera_cli.cloud.activity import ChatActivity, activity_counts
from alkera_cli.cloud.org_namespace import (
    CHAT_SLICE,
    LINK_WAIT_SECONDS,
    OrgNamespaceError,
    OrgNetwork,
    chat_ruleset,
    localdev_forward_argv,
    localdev_ports,
    prepare,
)
from alkera_cli.cloud.org_worker import (
    OrgWorkerError,
    read_hello,
    refuse_stored_login,
    status_of,
    worker_settings,
)
from alkera_cli.cloud.rest import CloudRestClient
from alkera_cli.cloud.worker_credential import WorkerCredential
from alkera_cli.harness.sandbox import METADATA_IPV4, chat_network_rules
from alkera_cli.org_root import OrgRoot
from alkera_cli.org_worker_protocol import Hello, Route, encode

ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
OTHER = "be0a3393-082f-4acf-918d-6eac00904c9b"


def _chat(chat_id: str, org: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"id": chat_id, "machine_id": "machine:x", "last_seq": 1}
    if org is not None:
        row["org_id"] = org
    return row


def _worker_service(tmp_path: Path) -> tuple[Any, list[tuple[str, str]]]:
    service, _built = build_service(tmp_path, clock=Clock(), org_id=ORG, machine_id="machine:x")
    service._machine_id = "machine:x"
    said: list[tuple[str, str]] = []
    assert service.org is not None
    service.org.on_refused = lambda chat_id, reason: said.append((chat_id, reason))
    return service, said


def test_a_worker_serves_a_routed_chat_whose_row_names_its_org(tmp_path: Path) -> None:
    service, said = _worker_service(tmp_path)
    service.org.route(["c1"])
    assert service._serves(_chat("c1", ORG)) is True
    assert service._serves(_chat("c1", ORG.upper())) is True
    assert said == []


@pytest.mark.parametrize(
    ("org", "reason"),
    [
        pytest.param(OTHER, "names another org", id="another-org"),
        pytest.param(None, "names another org", id="no-org-on-the-row"),
        pytest.param("not-a-uuid", "names another org", id="malformed-org"),
    ],
)
def test_a_routed_chat_whose_row_names_another_org_is_refused(
    tmp_path: Path, org: str | None, reason: str
) -> None:
    """The supervisor's routing is not trusted on its own: a routing mistake
    fails closed here, on the server's own row."""
    service, said = _worker_service(tmp_path)
    service.org.route(["c1"])
    assert service._serves(_chat("c1", org)) is False
    assert said and reason in said[0][1]


def test_a_chat_of_its_org_the_supervisor_did_not_route_is_refused(tmp_path: Path) -> None:
    service, said = _worker_service(tmp_path)
    assert service._serves(_chat("c1", ORG)) is False  # nothing routed yet
    service.org.route(["c2"])
    assert service._serves(_chat("c1", ORG)) is False
    assert service._serves(_chat("c2", ORG)) is True
    assert {chat for chat, _ in said} == {"c1"}


def test_a_box_that_is_not_a_worker_is_unchanged(tmp_path: Path) -> None:
    service, _built = build_service(tmp_path, clock=Clock())
    service._machine_id = "machine:x"
    assert service._serves(_chat("c1", OTHER)) is True
    assert service._serves(_chat("c2", None)) is True


@pytest.mark.parametrize(
    ("org_id", "machine_calls"),
    [
        pytest.param(ORG, False, id="org-worker-never-claims-nor-beats"),
        pytest.param(None, True, id="a-whole-box-registers-and-beats"),
    ],
)
async def test_a_started_org_worker_never_speaks_on_the_machine_routes(
    tmp_path: Path, org_id: str | None, machine_calls: bool
) -> None:
    """The supervisor claims and beats; the worker's org-bound credential is
    refused (as not-found) on the machine's own routes. A worker that beat
    anyway read the refusal as its machine being gone, dropped the machine id
    it was handed, and from then on served every chat in the org and never
    told the server it held a chat's session (the chat read asleep)."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(404, json={"detail": "Machine not found"})

    rest = CloudRestClient(
        api_url="http://127.0.0.1:1",
        token="t",
        agent_id="machine:x",
        transport=httpx.MockTransport(handler),
    )
    service, _built = build_service(
        tmp_path,
        clock=Clock(),
        rest=rest,
        org_id=org_id,
        machine_id="8ab073cf-f2f9-47ce-9ee1-aea16f45cee9" if org_id else None,
        heartbeat_interval=0.01,
        missing_route_retry=0.01,
        poll_interval=3600.0,
    )
    await service.start()
    try:
        await asyncio.sleep(0.3)
    finally:
        await service.stop()
    machine_paths = [path for path in asked if path.startswith("/api/v1/machines/")]
    assert bool(machine_paths) is machine_calls, machine_paths
    if org_id is not None:
        assert service.machine_id == "8ab073cf-f2f9-47ce-9ee1-aea16f45cee9"


def test_worker_settings_take_the_credential_from_the_hello_never_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALKERA_API_URL", "https://api.example")
    monkeypatch.setenv("ALKERA_MACHINE_CREDENTIAL", "alkm_from_env")
    hello = Hello(org_id=ORG, slot=4, machine_id="m-1", credential="alkm_from_hello")
    settings = worker_settings(hello, project_dir=tmp_path)
    assert settings.token == settings.machine_credential == "alkm_from_hello"
    assert (settings.org_id, settings.machine_id) == (ORG, "m-1")


def test_worker_settings_refuse_a_worker_with_no_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALKERA_API_URL", raising=False)
    with pytest.raises(OrgWorkerError):
        worker_settings(
            Hello(org_id=ORG, slot=0, machine_id="m", credential="c"), project_dir=tmp_path
        )


def test_a_stored_login_in_the_worker_s_home_is_refused(tmp_path: Path) -> None:
    refuse_stored_login(tmp_path)
    (tmp_path / "auth.yml").write_text("token: t\n")
    with pytest.raises(OrgWorkerError):
        refuse_stored_login(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="socketpair fds")
def test_the_first_frame_must_be_hello(tmp_path: Path) -> None:
    mine, theirs = socket.socketpair()
    try:
        mine.sendall(encode(Hello(org_id=ORG, slot=1, machine_id="m", credential="c")))
        assert read_hello(theirs.fileno()).org_id == ORG
        mine.sendall(encode(Route(chat_ids=("c1",))))
        with pytest.raises(OrgWorkerError):
            read_hello(theirs.fileno())
        mine.close()
        with pytest.raises(OrgWorkerError):
            read_hello(theirs.fileno())
    finally:
        theirs.close()


def test_the_org_s_chat_firewall_is_the_box_s_chat_rules_behind_a_closed_input() -> None:
    ruleset = chat_ruleset()
    for rule in chat_network_rules():
        assert rule in ruleset
    input_chain = ruleset[ruleset.index("chain input") : ruleset.index("chain forward")]
    assert "policy drop" in input_chain and input_chain.rstrip().endswith("}")
    assert f"ip daddr {METADATA_IPV4} counter drop" in ruleset[ruleset.index("chain output") :]


def test_every_set_the_org_s_chat_firewall_names_is_declared_in_it() -> None:
    """nft refuses a whole ruleset whose rule names a set the table lacks:
    every org worker then crashed on "could not load the org's chat
    firewall" and no chat on the box was served."""
    ruleset = chat_ruleset()
    named = set(re.findall(r"@([a-z0-9_]+)", ruleset))
    declared = set(re.findall(r"^\s*set ([a-z0-9_]+) \{", ruleset, flags=re.MULTILINE))
    assert named, "the chat rules name the box's sets"
    assert named <= declared, f"named but not declared: {sorted(named - declared)}"


def test_prepare_fills_the_egress_allowlist_with_the_box_s_private_endpoints() -> None:
    ran: list[tuple[str, ...]] = []
    written: dict[Path, str] = {}

    def run(argv: Any) -> int:
        ran.append(tuple(argv))
        return 0

    prepare(
        OrgNetwork(worker_ip="10.204.0.10", host_ip="10.204.0.9"),
        pid=7,
        run=run,
        write=lambda p, t: written.__setitem__(p, t),
        sleep=lambda _s: None,
        resolvers=lambda: ("172.31.0.2",),
        env={"ALKERA_API_URL": "http://api.internal:28180"},
        resolve=lambda host, port: ("192.168.65.254",) if host == "api.internal" else (),
    )
    allow = written[Path("/run/alkera-chat-allow.nft")]
    assert "192.168.65.254 . tcp . 28180" in allow
    assert "172.31.0.2 . udp . 53" in allow
    loads = [a for a in ran if a[:2] == ("nft", "-f")]
    assert loads == [
        ("nft", "-f", "/run/alkera-chat.nft"),
        ("nft", "-f", "/run/alkera-chat-allow.nft"),
    ]


def test_a_refused_allowlist_stops_the_worker() -> None:
    def run(argv: Any) -> int:
        return 1 if tuple(argv) == ("nft", "-f", "/run/alkera-chat-allow.nft") else 0

    with pytest.raises(OrgNamespaceError, match="egress allowlist"):
        prepare(
            OrgNetwork(worker_ip="10.204.0.2", host_ip="10.204.0.1"),
            pid=1,
            run=run,
            write=lambda p, t: None,
            sleep=lambda _s: None,
            resolvers=lambda: (),
            env={},
        )


def test_prepare_runs_its_steps_in_order_and_waits_for_the_link() -> None:
    ran: list[tuple[str, ...]] = []
    link_checks = iter([1, 1, 0])

    def run(argv: Any) -> int:
        argv = tuple(argv)
        if argv == ("ip", "link", "show", "eth0"):
            return next(link_checks)
        ran.append(argv)
        return 0

    written: dict[Path, str] = {}
    prepare(
        OrgNetwork(worker_ip="10.204.0.10", host_ip="10.204.0.9"),
        pid=77,
        run=run,
        write=lambda p, t: written.__setitem__(p, t),
        sleep=lambda _s: None,
        resolvers=lambda: ("172.31.0.2",),
    )
    joined = ran.index(
        ("/bin/sh", "-c", 'echo "$1" > "$2"', "join", "77", "/sys/fs/cgroup/worker/cgroup.procs")
    )
    handed = next(
        i
        for i, a in enumerate(ran)
        if a[-1] == f"/sys/fs/cgroup/{CHAT_SLICE}/cgroup.subtree_control"
    )
    assert (
        ran.index(("mount", "-t", "tmpfs", "-o", "nosuid,nodev,mode=0755", "tmpfs", "/run"))
        < joined
        < handed
    )
    assert ("ip", "addr", "add", "10.204.0.10/30", "dev", "eth0") in ran
    assert ("ip", "route", "add", "default", "via", "10.204.0.9") in ran
    assert ran[-1][:2] == ("nft", "-f") and written
    # The host's upstream resolvers, read before the worker's own /run hid them,
    # are where the host's /etc/resolv.conf points.
    for name in ("resolv.conf", "stub-resolv.conf"):
        assert written[Path("/run/systemd/resolve") / name] == "nameserver 172.31.0.2\n"


def test_a_host_with_no_upstream_resolver_writes_none() -> None:
    written: dict[Path, str] = {}
    prepare(
        OrgNetwork(worker_ip="10.204.0.2", host_ip="10.204.0.1"),
        pid=1,
        run=lambda argv: 0,
        write=lambda p, t: written.__setitem__(p, t),
        resolvers=lambda: (),
    )
    assert not any("resolve" in str(path) for path in written)


def test_prepare_gives_up_when_the_link_never_comes() -> None:
    now = [0.0]

    def tick(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(OrgNamespaceError):
        prepare(
            OrgNetwork(worker_ip="10.204.0.2", host_ip="10.204.0.1"),
            pid=1,
            run=lambda argv: 1 if tuple(argv)[:3] == ("ip", "link", "show") else 0,
            write=lambda p, t: None,
            sleep=tick,
            clock=lambda: now[0],
            resolvers=lambda: (),
        )
    assert now[0] >= LINK_WAIT_SECONDS


def test_a_failed_mount_stops_the_worker() -> None:
    with pytest.raises(OrgNamespaceError):
        prepare(
            OrgNetwork(worker_ip="a", host_ip="b"),
            pid=1,
            run=lambda argv: 1,
            write=lambda p, t: None,
        )


@pytest.mark.parametrize("raw", ["22900 x", "0", "70000", "-1", "22900;rm"])
def test_a_forward_port_that_is_not_a_port_refuses_every_forward(raw: str) -> None:
    with pytest.raises(OrgNamespaceError):
        localdev_ports(raw)


def test_forwards_reach_the_developer_machine_on_the_named_ports() -> None:
    assert localdev_ports("22900 22918") == (22900, 22918)
    assert localdev_forward_argv(22900) == (
        "socat",
        "TCP-LISTEN:22900,bind=127.0.0.1,fork,reuseaddr",
        "TCP:host.docker.internal:22900",
    )


async def test_an_org_worker_never_beats_or_claims_for_the_machine(tmp_path: Path) -> None:
    """The supervisor claims the machine and beats for it. A worker that did
    too, on its own credential, was refused, forgot the machine it publishes
    as, and from then on the platform read every chat it held as asleep."""
    service, _said = _worker_service(tmp_path)
    calls: list[str] = []

    async def _refused(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("machine call")
        raise AssertionError("an org worker called a machine route")

    service._rest.heartbeat_machine = _refused
    service._rest.claim_machine = _refused
    service._rest.register_machine = _refused

    await asyncio.wait_for(service._machine_loop(), timeout=5)

    assert calls == []
    assert service._machine_id == "machine:x"


@pytest.mark.parametrize(
    ("profile", "prepared"),
    [
        pytest.param("org_namespaces", True, id="namespaced-prepares-its-namespaces"),
        pytest.param(None, True, id="unsaid-is-what-every-worker-was-started-in"),
        pytest.param("single_org", False, id="single-org-has-none-to-prepare"),
    ],
)
def test_a_worker_prepares_namespaces_only_when_it_was_started_in_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str | None, prepared: bool
) -> None:
    """A single-org box's worker shares the host's namespaces: making its
    /run private or moving its link would fail there, every start."""
    calls: list[int] = []
    monkeypatch.setattr(org_worker, "prepare", lambda net, *, pid: calls.append(pid))
    monkeypatch.setenv("ALKERA_ORG_WORKER_IP", "10.204.0.10")
    monkeypatch.setenv("ALKERA_ORG_HOST_IP", "10.204.0.9")
    monkeypatch.delenv("ALKERA_LOCALDEV_FORWARD_PORTS", raising=False)
    if profile is None:
        monkeypatch.delenv("ALKERA_ORG_ISOLATION", raising=False)
    else:
        monkeypatch.setenv("ALKERA_ORG_ISOLATION", profile)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "org").mkdir()
    org_worker.prepare_worker(OrgRoot(tmp_path / "org"))
    assert bool(calls) is prepared


class _Holding:
    """A worker's service holding chats in the given activities."""

    def __init__(self, *held: ChatActivity) -> None:
        self.mirrors = {f"chat-{i}": a for i, a in enumerate(held)}

    def activity_counts(self) -> dict[str, int]:
        return activity_counts(self.mirrors.values())


def test_a_workers_status_counts_what_a_drain_would_wait_for() -> None:
    """The supervisor's status file sums these for the roll: a parked ask is
    busy but not working, and a background job is working."""
    held = _Holding(
        ChatActivity.WORKING,
        ChatActivity.RUNNING_JOB,
        ChatActivity.AWAITING_USER,
        ChatActivity.IDLE,
    )
    status = status_of(held, WorkerCredential("alkm_org.x"))  # type: ignore[arg-type]
    assert (status.chats_served, status.chats_busy, status.chats_working) == (4, 3, 2)
