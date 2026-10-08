"""Where a connection opened by another machine may reach is one policy.

The open platform refuses private ranges unless the operator names them in
``EGRESS_PRIVATE_ALLOWLIST``; a distribution registers at most one policy that
admits them. Which hosts a connector dials is read from registered sources, and a
connector nothing knows is refused.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from alkera_core.connectors.reach import (
    OPEN_REACH_POLICY,
    ConnectionReachPolicy,
    Endpoint,
    EndpointSource,
    ReachRefusedError,
    VettedEndpoint,
    endpoints,
    host_reach_refusal,
    reach_policy,
    vet_endpoints,
)
from alkera_core.egress import EgressPolicy
from alkera_core.extensions import ExtensionError, ExtensionPoint

PRODUCT = ConnectionReachPolicy(admits_private_ranges=True)
NO_OPT_IN = EgressPolicy()
GENERIC = "generic_sql"


def _url(host: str) -> dict[str, str]:
    return {"url": f"postgresql+psycopg://u@{host}/db"}


def _resolver(answers: Mapping[str, list[str]]) -> Callable[[str], list[str]]:
    def resolve(host: str) -> list[str]:
        if host not in answers:
            raise OSError(host)
        return answers[host]

    return resolve


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("10.0.1.23", id="rfc1918-10"),
        pytest.param("172.16.4.4", id="rfc1918-172"),
        pytest.param("192.168.1.10", id="rfc1918-192"),
        pytest.param("100.64.0.9", id="shared-address-space"),
        pytest.param("fd12:3456::1", id="unique-local"),
        pytest.param("::ffff:10.0.0.1", id="mapped-private"),
    ],
)
def test_the_open_policy_refuses_a_private_range(address: str) -> None:
    refusal = host_reach_refusal(address, policy=NO_OPT_IN, reach=OPEN_REACH_POLICY)
    assert refusal is not None
    assert "EGRESS_PRIVATE_ALLOWLIST" in refusal
    assert host_reach_refusal(address, policy=NO_OPT_IN, reach=PRODUCT) is None


def test_a_name_resolving_into_a_private_range_is_refused_by_default() -> None:
    resolve = _resolver({"db.corp.example": ["10.20.0.5"]})
    with pytest.raises(ReachRefusedError, match="private network address"):
        vet_endpoints(
            GENERIC,
            _url("db.corp.example"),
            resolve=resolve,
            policy=NO_OPT_IN,
            reach=OPEN_REACH_POLICY,
        )
    vetted = vet_endpoints(
        GENERIC, _url("db.corp.example"), resolve=resolve, policy=NO_OPT_IN, reach=PRODUCT
    )
    assert vetted == [VettedEndpoint("db.corp.example", ("10.20.0.5",))]


def test_the_operator_admits_a_private_network() -> None:
    allowed = EgressPolicy.from_entries(["10.20.0.0/16"])
    resolve = _resolver({"db.corp.example": ["10.20.0.5"], "other.corp.example": ["10.30.0.5"]})
    vetted = vet_endpoints(
        GENERIC,
        _url("db.corp.example"),
        resolve=resolve,
        policy=allowed,
        reach=OPEN_REACH_POLICY,
    )
    assert vetted == [VettedEndpoint("db.corp.example", ("10.20.0.5",))]
    with pytest.raises(ReachRefusedError):
        vet_endpoints(
            GENERIC,
            _url("other.corp.example"),
            resolve=resolve,
            policy=allowed,
            reach=OPEN_REACH_POLICY,
        )


@pytest.mark.parametrize("reach", [OPEN_REACH_POLICY, PRODUCT], ids=["open", "product"])
@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "::1", "0.0.0.0"])
def test_no_policy_admits_the_machine_itself(reach: ConnectionReachPolicy, address: str) -> None:
    assert host_reach_refusal(address, policy=NO_OPT_IN, reach=reach) is not None


def test_a_public_server_is_admitted_by_the_open_policy() -> None:
    resolve = _resolver({"db.example.com": ["93.184.216.34"]})
    vetted = vet_endpoints(
        GENERIC,
        _url("db.example.com"),
        resolve=resolve,
        policy=NO_OPT_IN,
        reach=OPEN_REACH_POLICY,
    )
    assert vetted == [VettedEndpoint("db.example.com", ("93.184.216.34",))]


def test_with_no_policy_registered_the_open_one_applies() -> None:
    assert reach_policy(ExtensionPoint("test.reach")) == OPEN_REACH_POLICY


def test_a_registered_policy_applies() -> None:
    point: ExtensionPoint[ConnectionReachPolicy] = ExtensionPoint("test.reach")
    point.register(PRODUCT)
    assert reach_policy(point) is PRODUCT


def test_two_registered_policies_are_refused() -> None:
    point: ExtensionPoint[ConnectionReachPolicy] = ExtensionPoint("test.reach")
    point.register(PRODUCT)
    point.register(ConnectionReachPolicy(admits_private_ranges=False))
    with pytest.raises(ExtensionError, match="exactly one"):
        reach_policy(point)


# -- endpoint sources ---------------------------------------------------------------


def _empty() -> ExtensionPoint[EndpointSource]:
    return ExtensionPoint("test.endpoints")


def test_a_generic_url_names_its_own_host_with_no_source() -> None:
    assert endpoints(GENERIC, _url("db.example.com"), sources=_empty()) == [
        Endpoint("db.example.com")
    ]


def test_a_multi_host_generic_url_names_every_host() -> None:
    found = endpoints(GENERIC, _url("a.example.com,b.example.com"), sources=_empty())
    assert found == [Endpoint("a.example.com"), Endpoint("b.example.com")]


@pytest.mark.parametrize(
    ("attributes", "message"),
    [
        pytest.param({"url": "sqlite:///data.db"}, "names no host", id="no-host"),
        pytest.param({"url": "not a url"}, "could not be parsed", id="unparseable"),
    ],
)
def test_a_generic_url_without_a_server_names_no_endpoint(
    attributes: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        endpoints(GENERIC, attributes, sources=_empty())


def test_a_connector_nothing_knows_is_refused() -> None:
    with pytest.raises(ValueError, match="no connector 'snowflake'"):
        endpoints("snowflake", {"account": "xy12345"}, sources=_empty())


def test_a_registered_source_answers_for_its_connector() -> None:
    def vendor(plugin: str, attributes: Mapping[str, Any]) -> list[Endpoint] | None:
        if plugin != "snowflake":
            return None
        return [Endpoint(f"{attributes['account']}.snowflakecomputing.com")]

    point = _empty()
    point.register(vendor)
    assert endpoints("snowflake", {"account": "xy12345"}, sources=point) == [
        Endpoint("xy12345.snowflakecomputing.com")
    ]
    with pytest.raises(ValueError, match="no connector 'mysql'"):
        endpoints("mysql", {"host": "db.example.com"}, sources=point)
