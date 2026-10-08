"""The BYOK ``base_url`` is an egress target, so its validation is a security control.

Whatever an org admin writes here becomes the host the model gateway POSTs a chat
turn to, and the upstream's response body is streamed back to the caller verbatim
(a non-200 surfaces its first bytes in the error envelope). An unvalidated value
is therefore a FULL-READ server-side request forgery, not a blind one — and an org
admin is an ordinary employee role, not an infrastructure operator.

The concrete escalation is the cloud instance-metadata service: pointing the
deployment's own egress at it hands back the infrastructure role the process runs
as, which is strictly larger than anything the application itself holds.
"""

from __future__ import annotations

import pytest
from alkera_core.schemas.tenancy.model_providers import (
    ModelProviderUpdateRequest,
    validate_provider_base_url,
)
from pydantic import ValidationError


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://169.254.169.254/latest/meta-data/", id="ec2-imds"),
        pytest.param("http://169.254.170.2/v2/credentials/abc", id="ecs-task-role-credentials"),
        pytest.param("https://169.254.169.254/computeMetadata/v1/", id="imds-over-https"),
        pytest.param("http://[fe80::1]/", id="ipv6-link-local"),
        pytest.param("http://metadata.google.internal/computeMetadata/v1/", id="gcp-metadata-host"),
        pytest.param("http://METADATA.GOOGLE.INTERNAL/", id="gcp-metadata-host-uppercase"),
        pytest.param("http://metadata.google.internal./", id="gcp-metadata-host-trailing-dot"),
        pytest.param("http://0.0.0.0/", id="unspecified"),
        pytest.param("http://224.0.0.1/", id="multicast"),
        pytest.param("http://240.0.0.1/", id="reserved"),
        pytest.param("http://[::ffff:169.254.169.254]/", id="ipv4-mapped-imds"),
        pytest.param("http://[2002:a9fe:a9fe::1]/", id="sixtofour-imds"),
        # The three IPv6 wrappers a hand-rolled `ipv4_mapped`/`sixtofour` pair
        # does not read through. NAT64 and IPv4-compatible both deliver to the
        # embedded v4 target on a network that routes them, and a Teredo address
        # names its server in the clear.
        pytest.param("http://[64:ff9b::a9fe:a9fe]/", id="nat64-imds"),
        pytest.param("http://[::a9fe:a9fe]/", id="ipv4-compatible-imds"),
        pytest.param("http://[2001:0:a9fe:a9fe::]/", id="teredo-imds"),
    ],
)
def test_an_infrastructure_target_is_refused(url: str) -> None:
    """The escalation targets. None of these is ever a legitimate LLM endpoint,
    and each one reachable from the gateway is a credential or an internal
    service the tenant was never granted."""
    with pytest.raises(ValueError):
        validate_provider_base_url(url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://2852039166/latest/meta-data/", id="imds-as-a-decimal-integer"),
        pytest.param("http://0xA9FEA9FE/", id="imds-as-hex"),
        pytest.param("http://0251.0376.0251.0376/", id="imds-as-dotted-octal"),
        pytest.param("http://169.254.43518/", id="imds-as-a-short-inet-aton-form"),
        pytest.param("http://2852039170/v2/credentials/abc", id="ecs-task-role-as-decimal"),
        pytest.param("http://0/", id="unspecified-as-an-integer"),
    ],
)
def test_a_respelled_address_is_refused_too(url: str) -> None:
    """A refusal that can be stepped around by respelling is not a refusal.

    ``ipaddress.ip_address`` accepts only the canonical dotted quad, but the OS
    resolver ALSO accepts every legacy ``inet_aton`` form — decimal, hex, octal,
    and the short two/three-part variants — and each of these resolves to exactly
    the address the dotted-quad rule refuses. Left as "hostnames" they reach the
    instance-metadata service with a one-character-different URL."""
    with pytest.raises(ValueError):
        validate_provider_base_url(url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("file:///etc/passwd", id="file-scheme"),
        pytest.param("gopher://x/_payload", id="gopher-scheme"),
        pytest.param("ftp://files.example/", id="ftp-scheme"),
        pytest.param("//api.anthropic.com", id="scheme-relative"),
        pytest.param("api.anthropic.com", id="bare-host"),
        pytest.param("https://", id="no-host"),
        pytest.param("https://user:pw@api.anthropic.com", id="embedded-credentials"),
        pytest.param("https://api.example/v1?next=", id="query-string"),
        pytest.param("https://api.example/v1#frag", id="fragment"),
        pytest.param("   ", id="blank"),
        # Bytes two URL parsers read differently. A backslash is a separator to a
        # browser-grade parser and an ordinary character to a strict one, so
        # whoever reads it second decides the host; whitespace and control
        # characters smuggle a second request line into the same connection.
        pytest.param("https://api.anthropic.com\\@169.254.169.254/", id="backslash-authority"),
        pytest.param("http://169.254.169.254\t/", id="tab-in-the-host"),
        pytest.param("https://api.example\r\nHost: evil/", id="crlf"),
        pytest.param("https://ex%61mple.com/", id="percent-encoded-host"),
        pytest.param("https://api.example:0/v1", id="port-zero"),
        pytest.param("https://api.example:99999/v1", id="port-out-of-range"),
    ],
)
def test_a_malformed_or_smuggling_url_is_refused(url: str) -> None:
    """Shape matters beyond the host. Non-HTTP schemes reach things the transport
    was never meant to speak to; embedded credentials smuggle an authenticator the
    operator never configured; and because the transport APPENDS ``/v1/messages``
    to this string, a query or fragment silently rewrites the path that is
    actually requested."""
    with pytest.raises(ValueError):
        validate_provider_base_url(url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://api.anthropic.com", id="the-real-provider"),
        pytest.param("https://llm.acme.dev/v1", id="a-corporate-proxy"),
        pytest.param("https://gw.acme.internal", id="an-internal-hostname"),
        pytest.param("https://api.example:8443/v1", id="a-non-default-port"),
        # Deliberately allowed: a self-hosted install legitimately points BYOK at
        # an in-VPC or same-host inference proxy. Refusing these would break the
        # supported deployment; the residual risk needs an operator setting plus
        # post-DNS pinning at the transport, not a request-schema rule.
        pytest.param("http://10.1.2.3:8000/v1", id="a-private-range-proxy"),
        pytest.param("http://localhost:8000", id="a-same-host-proxy"),
        pytest.param("http://127.0.0.1:8000", id="loopback-literal"),
        # The asymmetric edge of the numeric-spelling rule: digits and a leading
        # "0x" are perfectly ordinary INSIDE a name. Only the rightmost label
        # decides, because only that spelling is reserved for addresses.
        pytest.param("https://gpt4.example.com/v1", id="digits-in-a-label"),
        pytest.param("https://0xdeadbeef.example.com", id="a-hex-looking-first-label"),
        pytest.param("https://10-1-2-3.llm.acme.dev", id="an-address-shaped-name"),
        pytest.param("https://2852039166.acme.dev", id="an-all-digit-label-that-is-not-last"),
    ],
)
def test_a_legitimate_endpoint_still_validates(url: str) -> None:
    """The asymmetric half: the guard is a targeted refusal, not a denylist that
    happens to catch real endpoints too."""
    assert validate_provider_base_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://169.254.169.254.nip.io/", id="a-wildcard-dns-name-for-imds"),
        pytest.param("https://imds.attacker.example/", id="an-attacker-controlled-record"),
    ],
)
def test_a_dns_name_that_resolves_to_a_refused_address_still_passes(url: str) -> None:
    """The KNOWN limit, pinned so nobody reads the rules above as a closed door.

    This validates a STRING, never a resolved address: a name whose A record
    points at the metadata service — or one that only answers that way after the
    request is accepted — is indistinguishable here from a real provider. So the
    literal and metadata-hostname rules bound the direct spellings, which is what
    a request schema can do; the escalation is only fully closed by resolve-time
    IP pinning at the transport. When that lands, this test is the one that
    changes."""
    assert validate_provider_base_url(url) == url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, None, id="omitted"),
        pytest.param("", None, id="empty-string"),
        pytest.param("   ", None, id="whitespace-only"),
        pytest.param("  https://api.anthropic.com  ", "https://api.anthropic.com", id="padded"),
    ],
)
def test_the_request_schema_normalizes_absence_to_none(
    value: str | None, expected: str | None
) -> None:
    """ "Leave it at the default" must keep meaning None all the way through — the
    upsert service treats a blank as "no override", and a stray "" reaching the
    transport would concatenate into a relative URL."""
    assert ModelProviderUpdateRequest(base_url=value).base_url == expected


def test_the_request_schema_refuses_a_metadata_base_url() -> None:
    """End of the contract: the refusal is enforced at the HTTP boundary, so a
    PUT carrying it is a 422 and nothing is ever persisted for the gateway to
    dial."""
    with pytest.raises(ValidationError):
        ModelProviderUpdateRequest(api_key="sk-test", base_url="http://169.254.169.254/")
