"""A self-hosted install's box reaches any host it can; a hosted box stays strict.

Whether a box is a self-hosted install's is one deployment fact,
``CONNECTIONS_REACH_LOCAL_NETWORK`` (or an explicit ``SELF_HOSTED=true``), which the
one-machine compose stack sets and a platform box on a machine credential ignores.
There a connection reaches private ranges, the box's own loopback and the bridges
it sits on (the stack's own Postgres). Under every policy a metadata address and a
chat sandbox network are refused. Without the fact, the registered or open policy
judges as before, and a listed network never admits the box.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from alkera_core.connectors.reach import (
    OPEN_REACH_POLICY,
    SELF_HOSTED_REACH_POLICY,
    ConnectionReachPolicy,
    ReachRefusedError,
    VettedEndpoint,
    host_reach_refusal,
    reach_policy,
    reaches_local_network,
    vet_endpoints,
)
from alkera_core.egress import EgressPolicy, IPAddress
from alkera_core.extensions import ExtensionPoint

GENERIC = "generic_sql"
NO_OPT_IN = EgressPolicy()
#: The hosted product's registered policy: a customer's VPC is a server, the box is not.
HOSTED = ConnectionReachPolicy(admits_private_ranges=True)

#: The compose network the box sits on, and the chat sandbox block it carves.
BRIDGE = ipaddress.ip_network("172.18.0.0/16")
SANDBOXES = ipaddress.ip_network("10.200.0.0/14")


def _on_bridge(address: IPAddress) -> bool:
    return address.version == 4 and address in BRIDGE


def _in_sandbox(address: IPAddress) -> bool:
    return address.version == 4 and address in SANDBOXES


def _url(host: str) -> dict[str, str]:
    return {"url": f"postgresql+psycopg://u:p@{host}:5432/shop"}


def _resolver(answers: Mapping[str, list[str]]) -> Callable[[str], list[str]]:
    def resolve(host: str) -> list[str]:
        if host not in answers:
            raise OSError(host)
        return answers[host]

    return resolve


def _vet(
    host: str,
    answer: str,
    reach: ConnectionReachPolicy,
    policy: EgressPolicy = NO_OPT_IN,
) -> list[VettedEndpoint]:
    return vet_endpoints(
        GENERIC,
        _url(host),
        resolve=_resolver({host: [answer]}),
        is_local=lambda a: _on_bridge(a) or _in_sandbox(a),
        is_sandbox=_in_sandbox,
        policy=policy,
        reach=reach,
    )


# -- the self-hosted box ---------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "answer"),
    [
        pytest.param("postgres", "172.18.0.4", id="the-stacks-postgres-on-the-bridge"),
        pytest.param("warehouse.lan", "192.168.1.20", id="a-lan-host"),
        pytest.param("localhost", "127.0.0.1", id="the-boxs-loopback"),
        pytest.param("db.example.com", "203.0.113.7", id="a-public-server"),
    ],
)
def test_a_self_hosted_box_reaches_the_host(host: str, answer: str) -> None:
    assert _vet(host, answer, SELF_HOSTED_REACH_POLICY) == [VettedEndpoint(host, (answer,))]


@pytest.mark.parametrize(
    ("host", "answer"),
    [
        pytest.param("postgres", "172.18.0.4", id="the-bridge"),
        pytest.param("localhost", "127.0.0.1", id="the-loopback"),
    ],
)
def test_a_hosted_box_refuses_its_own_network(host: str, answer: str) -> None:
    with pytest.raises(ReachRefusedError, match="machine itself"):
        _vet(host, answer, HOSTED)


def test_the_open_default_still_refuses_a_lan_host() -> None:
    with pytest.raises(ReachRefusedError, match="private network address"):
        _vet("warehouse.lan", "192.168.1.20", OPEN_REACH_POLICY)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("169.254.169.254", id="aws-metadata"),
        pytest.param("::ffff:169.254.169.254", id="mapped-metadata"),
        pytest.param("169.254.10.1", id="link-local"),
        pytest.param("0.0.0.0", id="unspecified"),
        pytest.param("224.0.0.1", id="multicast"),
        pytest.param("10.200.0.170", id="another-chats-sandbox"),
    ],
)
@pytest.mark.parametrize(
    "reach", [SELF_HOSTED_REACH_POLICY, HOSTED, OPEN_REACH_POLICY], ids=["self", "hosted", "open"]
)
def test_no_policy_and_no_operator_name_reaches_metadata_or_a_sandbox(
    answer: str, reach: ConnectionReachPolicy
) -> None:
    named = EgressPolicy.from_entries(["evil.example.com"])
    with pytest.raises(ReachRefusedError, match="machine itself"):
        _vet("evil.example.com", answer, reach, policy=named)


@pytest.mark.parametrize(
    ("host", "self_hosted_admits"),
    [
        pytest.param("localhost", True, id="loopback-name"),
        pytest.param("127.0.0.1", True, id="loopback-address"),
        pytest.param("192.168.1.20", True, id="lan-address"),
        pytest.param("metadata.google.internal", False, id="metadata-name"),
        pytest.param("169.254.169.254", False, id="metadata-address"),
    ],
)
def test_the_saved_host_is_judged_the_same_way(host: str, self_hosted_admits: bool) -> None:
    refusal = host_reach_refusal(host, policy=NO_OPT_IN, reach=SELF_HOSTED_REACH_POLICY)
    assert (refusal is None) is self_hosted_admits
    assert host_reach_refusal(host, policy=NO_OPT_IN, reach=OPEN_REACH_POLICY) is not None


# -- the deployment fact -----------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "self_hosted", "env", "expected"),
    [
        pytest.param(True, None, {}, True, id="setting-on"),
        pytest.param(None, True, {}, True, id="explicit-self-hosted"),
        pytest.param(None, None, {}, False, id="inferred-self-hosted-is-not-enough"),
        pytest.param(False, True, {}, False, id="setting-off-wins"),
        pytest.param(
            True, True, {"ALKERA_MACHINE_CREDENTIAL": "mc_secret"}, False, id="platform-box"
        ),
    ],
)
def test_whether_a_box_reaches_its_local_network(
    monkeypatch: pytest.MonkeyPatch,
    setting: bool | None,
    self_hosted: bool | None,
    env: dict[str, str],
    expected: bool,
) -> None:
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "connections_reach_local_network", setting)
    monkeypatch.setattr(settings, "self_hosted", self_hosted)
    # With no Stripe keys the deployment infers it is self-hosted; a hosted box has
    # none either, so the inference must not open its network.
    monkeypatch.setattr(settings, "stripe_secret_key", None)
    assert reaches_local_network(env) is expected


@pytest.mark.parametrize(("on", "expected"), [(True, SELF_HOSTED_REACH_POLICY), (False, HOSTED)])
def test_the_deployment_fact_decides_over_the_registered_policy(
    monkeypatch: pytest.MonkeyPatch, on: bool, expected: ConnectionReachPolicy
) -> None:
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "connections_reach_local_network", on)
    monkeypatch.delenv("ALKERA_MACHINE_CREDENTIAL", raising=False)
    point: ExtensionPoint[ConnectionReachPolicy] = ExtensionPoint("test.reach.hosted")
    point.register(HOSTED)
    assert reach_policy(point) == expected


# -- a listed network never admits the box ------------------------------------------


@pytest.mark.parametrize(
    ("answer", "admitted"),
    [
        pytest.param("10.20.0.5", True, id="a-server-in-the-listed-network"),
        pytest.param("172.18.0.9", False, id="the-boxs-bridge-in-the-listed-network"),
    ],
)
def test_a_listed_network_never_admits_the_box(answer: str, admitted: bool) -> None:
    listed = EgressPolicy.from_entries(["10.0.0.0/8", "172.16.0.0/12"])
    if admitted:
        assert _vet("db.lan", answer, OPEN_REACH_POLICY, listed) == [
            VettedEndpoint("db.lan", (answer,))
        ]
        return
    with pytest.raises(ReachRefusedError, match="machine itself"):
        _vet("db.lan", answer, OPEN_REACH_POLICY, listed)


@pytest.mark.parametrize(
    ("entry", "host"),
    [
        pytest.param("127.0.0.0/8", "127.0.0.1", id="loopback-range"),
        pytest.param("127.0.0.1", "127.0.0.1", id="loopback-address"),
        pytest.param("127.0.0.0/8", "2130706433", id="loopback-integer-spelling"),
    ],
)
def test_a_listed_network_never_admits_the_loopback(entry: str, host: str) -> None:
    policy = EgressPolicy.from_entries([entry])
    assert host_reach_refusal(host, policy=policy, reach=OPEN_REACH_POLICY) is not None
    with pytest.raises(ReachRefusedError):
        vet_endpoints(
            GENERIC, _url(host), resolve=_resolver({}), policy=policy, reach=OPEN_REACH_POLICY
        )


# -- the one-machine compose stack --------------------------------------------------


def _compose() -> dict[str, Any]:
    """The open tree's ``compose.yaml``: this package sits at ``packages/api-core``
    under the open root, which is this checkout's root or its ``Databench`` folder."""
    root = Path(__file__).resolve().parents[4]
    for candidate in (root / "compose.yaml", root / "Databench" / "compose.yaml"):
        if candidate.is_file():
            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            assert isinstance(loaded, dict)
            return loaded
    raise AssertionError(f"no compose.yaml under {root}")


@pytest.mark.parametrize("service", ["box", "backend"])
def test_the_compose_stack_is_a_self_hosted_install(service: str) -> None:
    environment = _compose()["services"][service]["environment"]
    assert environment["SELF_HOSTED"] == "true"


def test_the_compose_boxs_sandboxes_cannot_overlap_a_docker_network() -> None:
    """Docker's default pools, and the private ranges a daemon's own pools are drawn
    from: a sandbox block inside one would refuse the stack's own services."""
    raw = _compose()["services"]["box"]["environment"]["ALKERA_SANDBOX_NET"]
    block = ipaddress.ip_network(raw, strict=True)
    assert block.prefixlen <= 14
    for pool in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert not block.overlaps(ipaddress.ip_network(pool)), pool
