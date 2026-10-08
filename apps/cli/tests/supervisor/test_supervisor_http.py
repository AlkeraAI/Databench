"""The supervisor's calls to the backend, against a real local HTTP server."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.supervisor.http import MAX_BODY_BYTES, Api, ApiError, ssl_context
from alkera_cli.supervisor.service import MachineApi, RouteEntry
from alkera_core.ca_bundle import OutboundTlsConfigError

ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
CREDENTIAL = "alkm_secret"


class Backend:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str, dict[str, str], Any]] = []
        self.answers: dict[tuple[str, str], tuple[int, bytes]] = {}


@pytest.fixture
def backend() -> Iterator[tuple[Backend, str]]:
    state = Backend()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            state.seen.append((self.command, self.path, dict(self.headers), body))
            status, payload = state.answers.get((self.command, self.path), (404, b"{}"))
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _serve  # noqa: N815 -- the handler's own spelling
        do_POST = _serve  # noqa: N815

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


async def test_every_call_carries_the_machine_credential(backend: tuple[Backend, str]) -> None:
    state, url = backend
    state.answers[("POST", "/api/v1/machines/me/worker-credentials")] = (
        201,
        json.dumps({"token": "alkm_org.x", "expires_in": 900}).encode(),
    )
    api = MachineApi(api_url=url, credential=CREDENTIAL)
    assert await api.worker_credential(ORG) == ("alkm_org.x", 900.0)
    _method, _path, headers, body = state.seen[-1]
    assert headers["Authorization"] == f"Bearer {CREDENTIAL}"
    assert CREDENTIAL in headers.values()
    assert body == {"org_id": ORG}
    assert CREDENTIAL not in repr(api)


async def test_the_routing_is_read_page_by_page(backend: tuple[Backend, str]) -> None:
    state, url = backend
    first = {"items": [{"chat_id": "a1", "org_id": ORG, "state": "awake"}], "next_cursor": "p2"}
    second = {"items": [{"chat_id": "a2", "org_id": ORG, "state": "asleep"}], "next_cursor": None}
    state.answers[("GET", "/api/v1/machines/me/routing?limit=500")] = (
        200,
        json.dumps(first).encode(),
    )
    state.answers[("GET", "/api/v1/machines/me/routing?limit=500&cursor=p2")] = (
        200,
        json.dumps(second).encode(),
    )
    entries = await MachineApi(api_url=url, credential=CREDENTIAL).read()
    assert list(entries) == [RouteEntry("a1", ORG, "open"), RouteEntry("a2", ORG, "asleep")]


async def test_an_error_status_and_an_unreachable_backend_are_api_errors(
    backend: tuple[Backend, str],
) -> None:
    _state, url = backend
    with pytest.raises(ApiError) as refused:
        await Api(url, {}).call("POST", "/api/v1/machines/claim", {})
    assert refused.value.status == 404
    with pytest.raises(ApiError) as down:
        await Api("http://127.0.0.1:1", {}).call("GET", "/")
    assert down.value.status == 0


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        pytest.param(b"not json", "not JSON", id="not-json"),
        pytest.param(b"x" * (MAX_BODY_BYTES + 1), "size", id="too-big"),
    ],
)
async def test_an_answer_that_is_not_what_was_asked_is_refused(
    backend: tuple[Backend, str], payload: bytes, error: str
) -> None:
    state, url = backend
    state.answers[("GET", "/x")] = (200, payload)
    with pytest.raises(ApiError, match=error):
        await Api(url, {}).call("GET", "/x")


async def test_a_claim_with_no_machine_id_is_refused(backend: tuple[Backend, str]) -> None:
    state, url = backend
    state.answers[("POST", "/api/v1/machines/claim")] = (200, b"{}")
    with pytest.raises(ValueError):
        await MachineApi(api_url=url, credential=CREDENTIAL).claim({"provider_pod_id": "i-1"})


def test_a_ca_bundle_that_is_not_a_certificate_is_refused(tmp_path: Any) -> None:
    bad = tmp_path / "ca.pem"
    bad.write_text("nope")
    with pytest.raises(Exception):  # noqa: B017 -- ssl raises its own error types
        ssl_context({"OUTBOUND_CA_BUNDLE": str(bad)})
    assert ssl_context({}) is not None


def _ca_pem() -> str:
    """A fresh self-signed CA, so the test can see it join the trust store."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "supervisor test CA")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.mark.parametrize(
    "form", [pytest.param("file", id="file"), pytest.param("inline", id="inline")]
)
def test_the_deployments_ca_joins_the_system_trust_store(tmp_path: Path, form: str) -> None:
    pem = _ca_pem()
    path = tmp_path / "ca.pem"
    path.write_text(pem)
    bundle = f"  {path}\n" if form == "file" else pem
    system = ssl_context({}).cert_store_stats()["x509_ca"]
    assert ssl_context({"OUTBOUND_CA_BUNDLE": bundle}).cert_store_stats()["x509_ca"] == system + 1


@pytest.mark.parametrize(
    ("bundle", "write"),
    [
        pytest.param("missing.pem", None, id="missing-file"),
        pytest.param("ca.pem", "nope", id="file-that-is-not-a-certificate"),
        pytest.param(".", None, id="a-directory"),
        pytest.param(
            "-----BEGIN CERTIFICATE-----\nnot-base64!!!\n-----END CERTIFICATE-----\n",
            None,
            id="inline-pem-that-does-not-decode",
        ),
    ],
)
def test_a_bad_ca_bundle_is_refused_naming_the_setting(
    tmp_path: Path, bundle: str, write: str | None
) -> None:
    if write is not None:
        (tmp_path / bundle).write_text(write)
    value = bundle if "BEGIN" in bundle else str(tmp_path / bundle)
    with pytest.raises(OutboundTlsConfigError, match="OUTBOUND_CA_BUNDLE"):
        ssl_context({"OUTBOUND_CA_BUNDLE": value})
