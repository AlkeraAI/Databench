"""``is_private_host``: the one reachability rule the gate status endpoint,
the CLI's upload hints, and the generated-workflow warnings share. Private
means the public internet (a GitHub-hosted runner, GitHub's webhook servers)
cannot dial it, so the product must warn instead of failing dark."""

from __future__ import annotations

import pytest
from alkera_core.net import is_private_host


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://localhost:8000", id="localhost"),
        pytest.param("http://api.localhost:8000", id="localhost-subdomain"),
        pytest.param("http://127.0.0.1:8000", id="loopback-v4"),
        pytest.param("http://127.5.4.3", id="loopback-v4-range"),
        pytest.param("https://[::1]:8443", id="loopback-v6"),
        pytest.param("http://0.0.0.0:8000", id="unspecified"),
        pytest.param("http://10.1.2.3", id="rfc1918-10"),
        pytest.param("http://172.16.0.9:9000", id="rfc1918-172-low-edge"),
        pytest.param("http://172.31.255.1", id="rfc1918-172-high-edge"),
        pytest.param("http://192.168.1.10:5173", id="rfc1918-192"),
        pytest.param("http://169.254.169.254", id="link-local"),
        pytest.param("http://100.64.0.7", id="cgnat-range"),
        pytest.param("https://backend.local", id="mdns-suffix"),
        pytest.param("https://api.internal:8000", id="internal-suffix"),
        pytest.param("http://devbox:8000", id="dotless-hostname"),
        pytest.param("not a url", id="unparsable"),
        pytest.param("", id="empty"),
    ],
)
def test_private_hosts(url: str) -> None:
    assert is_private_host(url) is True


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://api.example.org", id="saas"),
        pytest.param("https://api.example.com:8443", id="public-with-port"),
        pytest.param("http://8.8.8.8", id="public-ip"),
        pytest.param("http://172.32.0.1", id="just-past-rfc1918"),
        pytest.param("https://example.com.", id="trailing-dot"),
        pytest.param("https://user:pw@api.example.com/path", id="userinfo-and-path"),
    ],
)
def test_public_hosts(url: str) -> None:
    assert is_private_host(url) is False
