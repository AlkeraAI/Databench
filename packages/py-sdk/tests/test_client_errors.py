"""Typed errors on the hand-written wrapper.

A caller that has to read prose out of a ``RuntimeError`` to tell "your token
is gone" from "that folder is not there" cannot act on either. These pin the
two facts a caller acts on — the HTTP status class and the API's own error
code — as attributes on the exception, against a scripted transport driving
the real ``AlkeraClient``.
"""

from __future__ import annotations

import httpx
import pytest
from alkera_sdk import AlkeraClient
from alkera_sdk.client import AlkeraAuthError, AlkeraHTTPError


def _client(answer: httpx.Response) -> AlkeraClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer

    return AlkeraClient(
        base_url="https://api.test",
        token="tok",
        httpx_args={"transport": httpx.MockTransport(handler)},
    )


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(401, {"detail": "Not authenticated"}, id="401-expired-token"),
        pytest.param(403, {"code": "forbidden", "message": "no"}, id="403-insufficient"),
    ],
)
def test_a_rejected_credential_raises_the_auth_error(status: int, body: dict[str, str]) -> None:
    """401 and 403 are the auth class: a caller flips to a login prompt on them."""
    with _client(httpx.Response(status, json=body)) as api, pytest.raises(AlkeraAuthError) as seen:
        api.files.drive()

    assert seen.value.status == status
    assert isinstance(seen.value, AlkeraHTTPError)


def test_a_files_refusal_carries_the_api_code() -> None:
    """A 404 is NOT the auth class, and its code reaches the caller as data."""
    answer = httpx.Response(404, json={"code": "not_found"}, headers={"x-request-id": "rq-7"})
    with _client(answer) as api, pytest.raises(AlkeraHTTPError) as seen:
        api.files.drive()

    assert not isinstance(seen.value, AlkeraAuthError)
    assert (seen.value.status, seen.value.code, seen.value.trace_id) == (404, "not_found", "rq-7")


def test_a_conflict_carries_its_message_not_the_raw_body() -> None:
    """The message a caller shows is the API's sentence, not 200 bytes of JSON."""
    answer = httpx.Response(409, json={"code": "conflict", "message": "etag moved"})
    with _client(answer) as api, pytest.raises(AlkeraHTTPError) as seen:
        api.files.drive()

    assert (seen.value.code, seen.value.message) == ("conflict", "etag moved")


def test_the_typed_errors_stay_runtime_errors() -> None:
    """Existing callers catch ``RuntimeError``; the new types must not slip past them."""
    with _client(httpx.Response(401, json={})) as api, pytest.raises(RuntimeError):
        api.files.drive()


def test_a_good_answer_passes_through_untouched() -> None:
    """No error class on the success path — the body is the answer."""
    with _client(httpx.Response(200, json={"id": "drv-1"})) as api:
        assert api.files.drive() == {"id": "drv-1"}
