"""The box egress policy: what a chat or an org worker may reach off the box,
and the box ruleset that carries it (``sandbox_prereqs.sh``)."""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from pathlib import Path

import pytest
from alkera_core.compute import box_egress as egress
from alkera_core.compute.box_egress import AllowEntry
from alkera_core.net_ranges import DENIED

# The module-scoped rendered script is shared by every test here.
pytestmark = pytest.mark.xdist_group("box_egress")

SCRIPT = Path(egress.__file__).with_name("sandbox_prereqs.sh")


def _resolver(table: dict[str, Sequence[str]]) -> egress.Resolve:
    def resolve(host: str, port: int) -> Sequence[str]:
        return table.get(host, ())

    return resolve


# -- the deny sets cover every denied range ----------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "10.0.1.5",
        "10.255.255.254",
        "172.16.0.1",
        "172.31.255.1",
        "192.168.1.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "169.254.1.1",
        "0.250.250.254",
        "::1",
        "fd00:ec2::254",
        "fe80::1",
    ],
)
def test_every_private_loopback_link_local_and_metadata_address_is_denied(address: str) -> None:
    ip = ipaddress.ip_address(address)
    elements = egress.deny_elements(4 if ip.version == 4 else 6).split(", ")
    assert any(ip in ipaddress.ip_network(e) for e in elements)


@pytest.mark.parametrize("address", ["8.8.8.8", "52.94.236.248", "2606:4700::1111"])
def test_a_public_address_is_not_denied(address: str) -> None:
    ip = ipaddress.ip_address(address)
    elements = egress.deny_elements(4 if ip.version == 4 else 6).split(", ")
    assert not any(ip in ipaddress.ip_network(e) for e in elements)


def test_the_deny_sets_are_the_whole_shared_table() -> None:
    """The box denies what the server-side fetch guard denies, no less."""
    for network, _label in DENIED:
        version = 4 if network.version == 4 else 6
        elements = [ipaddress.ip_network(e) for e in egress.deny_elements(version).split(", ")]
        assert any(network.subnet_of(e) for e in elements), network  # type: ignore[arg-type]


# -- what the box itself needs is allowed, and only that -----------------------------


def test_a_private_backend_gateway_and_extra_endpoint_are_allowed_on_their_ports() -> None:
    env = {
        "ALKERA_API_URL": "https://api.internal",
        "ALKERA_GATEWAY_URL": "http://gw.internal:8081",
        egress.ENV_EXTRA_ENDPOINTS: "https://files.internal:9443, ",
    }
    resolve = _resolver(
        {"api.internal": ["10.0.1.5"], "gw.internal": ["10.0.1.6"], "files.internal": ["10.0.9.9"]}
    )
    entries = egress.allow_entries(egress.box_endpoints(env), (), resolve=resolve)
    assert entries == (
        AllowEntry("10.0.1.5", "tcp", 443),
        AllowEntry("10.0.1.6", "tcp", 8081),
        AllowEntry("10.0.9.9", "tcp", 9443),
    )


def test_a_public_endpoint_needs_no_entry() -> None:
    resolve = _resolver({"api.example.com": ["52.94.236.248"]})
    assert egress.allow_entries(["https://api.example.com"], (), resolve=resolve) == ()


@pytest.mark.parametrize("address", ["169.254.169.254", "169.254.1.1", "fd00:ec2::254", "fe80::1"])
def test_an_endpoint_that_resolves_to_metadata_or_link_local_opens_nothing(address: str) -> None:
    """A hostile or mistaken name cannot open the metadata service."""
    resolve = _resolver({"api.internal": [address]})
    assert egress.allow_entries(["https://api.internal"], (), resolve=resolve) == ()


def test_the_boxs_resolvers_are_allowed_on_port_53_only_never_metadata_or_loopback() -> None:
    entries = egress.allow_entries(
        (), ["10.0.0.2", "169.254.169.253", "169.254.169.254", "127.0.0.53", "8.8.8.8", "junk"]
    )
    assert entries == (
        AllowEntry("10.0.0.2", "tcp", 53),
        AllowEntry("10.0.0.2", "udp", 53),
        AllowEntry("169.254.169.253", "tcp", 53),
        AllowEntry("169.254.169.253", "udp", 53),
    )


def test_the_resolvers_come_from_the_first_file_that_names_one() -> None:
    files = {
        "/run/systemd/resolve/resolv.conf": "# none\n",
        "/etc/resolv.conf": "nameserver 10.0.0.2\nsearch x\nnameserver 10.0.0.3\n",
    }
    assert egress.box_resolvers(files.get) == ("10.0.0.2", "10.0.0.3")


def test_the_allowlist_replaces_both_sets_in_one_transaction() -> None:
    script = egress.allowlist_script(
        [AllowEntry("10.0.1.5", "tcp", 443), AllowEntry("fd12::5", "tcp", 443)]
    )
    assert script.splitlines() == [
        "flush set inet alkera_sandbox egress_allow_v4",
        "flush set inet alkera_sandbox egress_allow_v6",
        "add element inet alkera_sandbox egress_allow_v4 { 10.0.1.5 . tcp . 443 }",
        "add element inet alkera_sandbox egress_allow_v6 { fd12::5 . tcp . 443 }",
    ]


def test_off_a_box_nothing_is_applied() -> None:
    assert egress.apply_allowlist({"ALKERA_API_URL": "http://10.0.0.1"}, nft="/nonexistent") == ()


def test_on_a_box_without_nft_the_entries_are_still_named_and_nothing_raises() -> None:
    env = {"ALKERA_SANDBOX_MODE": "gvisor", "ALKERA_API_URL": "http://api.internal:8000"}
    entries = egress.apply_allowlist(
        env,
        resolve=_resolver({"api.internal": ["10.0.1.5"]}),
        read=lambda _path: None,
        nft="/nonexistent/nft",
    )
    assert entries == (AllowEntry("10.0.1.5", "tcp", 8000),)


# -- the ruleset carries this policy ------------------------------------------------


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _section(script: str, start: str, end: str) -> str:
    return script[script.index(start) : script.index(end)]


def test_the_ruleset_declares_the_deny_sets_with_this_policys_elements(script: str) -> None:
    for line in egress.deny_set_lines():
        assert f"  {line}\n" in script, line


@pytest.mark.parametrize(("link", "peers"), [("vc*", "${SANDBOX_NET}"), ("vo*", "${SANDBOX_NET}")])
def test_a_link_reaches_a_denied_address_only_through_the_allowlist(
    script: str, link: str, peers: str
) -> None:
    """After its metadata and peer drops, before the rule that sends the rest
    out: an allowed entry, else a drop of every denied range."""
    forward = _section(script, "chain forward {", "chain postrouting {")
    peer_drop = forward.index(f'iifname "{link}" ip daddr {peers} counter drop')
    out = forward.index(f'iifname "{link}" oifname != "{link}" accept')
    positions = [forward.index(rule) for rule in egress.forward_rules(link)]
    assert peer_drop < positions[0] and positions == sorted(positions) and positions[-1] < out


def test_a_chat_uid_on_the_host_network_meets_the_same_policy(script: str) -> None:
    output = _section(script, "chain output {", "chain input {")
    rules = [
        r.replace("meta skuid 1-2 ", "meta skuid ${CHAT_UID_MIN}-${CHAT_UID_MAX} ")
        for r in egress.output_rules(1, 2)
    ]
    metadata = output.index("ip6 daddr ${METADATA_IPV6} counter drop")
    positions = [output.index(rule) for rule in rules]
    assert metadata < positions[0] and positions == sorted(positions)
