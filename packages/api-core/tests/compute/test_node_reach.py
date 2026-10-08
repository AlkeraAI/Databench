"""A deployment with no public address refuses exactly the machines whose boot
profile says they call back from outside."""

from __future__ import annotations

import pytest
from alkera_core.compute.bootstrap import BOOT_PROFILES
from alkera_core.compute.node_reach import (
    NO_PUBLIC_ADDRESS,
    callback_refusal,
    is_public_address,
    loopback_callback_refusal,
)


@pytest.mark.parametrize("kind", sorted(BOOT_PROFILES))
def test_the_refusal_follows_the_boot_profile_alone(kind: str) -> None:
    expected = NO_PUBLIC_ADDRESS if BOOT_PROFILES[kind].needs_public_callback else None
    assert callback_refusal(kind, "http://localhost:28182") == expected
    assert callback_refusal(kind, "https://api.example.com") is None


@pytest.mark.parametrize(
    ("kind", "refused"),
    [
        pytest.param("runpod", True, id="runpod-calls-back-from-outside"),
        pytest.param("ec2", True, id="ec2-calls-back-from-outside"),
        pytest.param("localdev", False, id="a-local-box-reaches-the-host"),
        pytest.param("no-such-provider", False, id="no-profile-is-refused-at-its-launch"),
    ],
)
def test_each_provider_on_a_loopback_deployment(kind: str, refused: bool) -> None:
    assert (callback_refusal(kind, "http://127.0.0.1:8000") is not None) is refused


@pytest.mark.parametrize(
    ("url", "public"),
    [
        pytest.param("http://localhost:8000", False, id="localhost"),
        pytest.param("http://files.localhost:8000", False, id="dot-localhost"),
        pytest.param("http://box.local", False, id="dot-local"),
        pytest.param("http://host.docker.internal:8000", False, id="dot-internal"),
        pytest.param("http://10.0.0.5", False, id="private"),
        pytest.param("http://169.254.169.254", False, id="link-local"),
        pytest.param("http://[::1]:8000", False, id="ipv6-loopback"),
        pytest.param("http://devbox", False, id="bare-name"),
        pytest.param("", False, id="empty"),
        pytest.param("https://api.example.com", True, id="a-public-name"),
        pytest.param("https://abc.trycloudflare.com", True, id="a-tunnel"),
        pytest.param("http://8.8.8.8", True, id="a-public-address"),
    ],
)
def test_is_public_address(url: str, public: bool) -> None:
    assert is_public_address(url) is public


@pytest.mark.parametrize(
    ("url", "refused"),
    [
        pytest.param("http://localhost:8080", True, id="localhost"),
        pytest.param("http://app.localhost:8080", True, id="dot-localhost"),
        pytest.param("http://127.0.0.2:8080", True, id="loopback-range"),
        pytest.param("http://[::1]:8080", True, id="ipv6-loopback"),
        pytest.param("http://0.0.0.0:8080", True, id="unspecified"),
        pytest.param("", True, id="no-host"),
        # A private address may be routable from the host: the probe decides.
        pytest.param("http://10.0.0.5:8000", False, id="private"),
        pytest.param("http://host.docker.internal:8000", False, id="docker-host-name"),
        pytest.param("https://databench.example.com", False, id="public"),
    ],
)
def test_only_a_loopback_callback_is_refused_from_the_address_alone(
    url: str, refused: bool
) -> None:
    said = loopback_callback_refusal({"ALKERA_NODE_API_URL": url})
    assert (said is not None) is refused
