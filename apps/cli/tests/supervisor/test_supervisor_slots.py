"""The slot table: one slot per org, never two orgs on one id range."""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from pathlib import Path

import pytest
from alkera_cli.supervisor.slots import (
    MAX_SLOTS,
    ORG_NET,
    ORG_UID_BASE,
    ORG_UID_SPAN,
    Slot,
    SlotError,
    SlotTable,
    canonical_org,
)

ORG_A = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
ORG_B = "be0a3393-082f-4acf-918d-6eac00904c9b"


def test_an_org_keeps_its_slot_across_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "slots.json"
    first = SlotTable(path).assign(ORG_A)
    second = SlotTable(path).assign(ORG_B)
    again = SlotTable(path)
    assert (first.index, second.index) == (0, 1)
    assert again.find(ORG_A) == first and again.find(ORG_B) == second
    assert again.assign(ORG_A) == first


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param(ORG_A.upper(), id="upper"),
        pytest.param("{" + ORG_A + "}", id="braces"),
        pytest.param(ORG_A.replace("-", ""), id="no-dashes"),
    ],
)
def test_one_org_spelled_two_ways_takes_one_slot(tmp_path: Path, spelling: str) -> None:
    table = SlotTable(tmp_path / "slots.json")
    assert table.assign(ORG_A) == table.assign(spelling)
    assert len(table.slots()) == 1


@pytest.mark.parametrize("raw", ["", "acme", "../0", "8ef423bc"])
def test_a_string_that_is_not_an_org_id_takes_no_slot(tmp_path: Path, raw: str) -> None:
    with pytest.raises(SlotError):
        SlotTable(tmp_path / "slots.json").assign(raw)


def test_a_released_slot_is_reused_and_nothing_else_moves(tmp_path: Path) -> None:
    table = SlotTable(tmp_path / "slots.json")
    table.assign(ORG_A)
    b = table.assign(ORG_B)
    table.release(ORG_A)
    c = table.assign("11111111-2222-3333-4444-555555555555")
    assert c.index == 0 and table.find(ORG_B) == b


def test_the_id_ranges_and_links_of_two_slots_never_meet() -> None:
    first, last = Slot(0, ORG_A), Slot(MAX_SLOTS - 1, ORG_B)
    assert first.uid_base == ORG_UID_BASE
    assert len(first.uid_range) == ORG_UID_SPAN
    assert set(first.uid_range).isdisjoint(Slot(1, ORG_B).uid_range)
    assert last.uid_range.stop <= 2**31
    # The /30 of every slot fits the block and none share an address.
    hosts = {Slot(i, ORG_A).host_ip for i in range(MAX_SLOTS)}
    workers = {Slot(i, ORG_A).worker_ip for i in range(MAX_SLOTS)}
    assert len(hosts) == len(workers) == MAX_SLOTS and hosts.isdisjoint(workers)
    assert all(ipaddress.ip_address(a) in ORG_NET for a in hosts | workers)
    assert first.host_if == "vo0" and len(last.host_if) <= 15


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("not json", id="not-json"),
        pytest.param(json.dumps({"slots": []}), id="not-a-map"),
        pytest.param(json.dumps({"slots": {"0": ORG_A, "1": ORG_A}}), id="one-org-two-slots"),
        pytest.param(json.dumps({"slots": {str(MAX_SLOTS): ORG_A}}), id="slot-out-of-range"),
        pytest.param(json.dumps({"slots": {"0": "acme"}}), id="not-an-org"),
    ],
)
def test_a_table_that_is_not_what_was_written_stops_the_supervisor(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "slots.json"
    path.write_text(content)
    with pytest.raises(SlotError):
        SlotTable(path)


@pytest.mark.skipif(sys.platform == "win32", reason="mode bits are a POSIX concept")
def test_the_table_is_the_supervisor_s_alone(tmp_path: Path) -> None:
    path = tmp_path / "state" / "slots.json"
    previous = os.umask(0o022)
    try:
        SlotTable(path).assign(ORG_A)
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_canonical_org_is_the_uuid_spelling() -> None:
    assert canonical_org(ORG_A.upper()) == ORG_A


def test_before_any_worker_the_org_links_reach_the_allowlist_and_pass_dockers_forward_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supervisor prepares the host once before its first worker: the
    private addresses an org link may reach are filled, and on a host that
    also runs Docker the org links are accepted ahead of its forward drop."""
    from collections.abc import Mapping, Sequence

    from alkera_cli import org_slot
    from alkera_core.compute import box_egress as egress
    from alkera_core.compute import host_forward

    filled: list[Mapping[str, str]] = []
    docker_user: list[tuple[str, ...]] = []

    def iptables(argv: Sequence[str]) -> int:
        if argv[0] != "iptables" or argv[3] != "DOCKER-USER":
            return 1  # no ip6tables, ufw or firewalld on this host
        _binary, _wait, verb, _chain, *rule = argv
        if verb == "-C":
            return 0 if tuple(rule) in docker_user else 1
        if verb == "-I":
            docker_user.insert(int(rule[0]) - 1, tuple(rule[1:]))
        return 0

    real = host_forward.open_links
    monkeypatch.setattr(egress, "apply_allowlist", lambda env: filled.append(env) or ())
    monkeypatch.setattr(
        host_forward,
        "open_links",
        lambda env, prefixes: real(env, prefixes, host=host_forward.Host(run=iptables)),
    )
    env = {"ALKERA_SANDBOX_MODE": "gvisor"}

    org_slot.prepare_org_links(env)

    assert filled == [env]
    assert docker_user == list(host_forward.link_accepts((org_slot.ORG_VETH_PREFIX,)))
    assert docker_user[0][:2] == ("-i", "vo+")
