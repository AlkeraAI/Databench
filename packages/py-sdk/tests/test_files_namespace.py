"""The ``AlkeraClient.files`` namespace against a scripted transport.

The backend suite proves the routes; this suite proves the half of the Files
contract that lives in the *client*: the path and method each verb reaches for,
the ``Idempotency-Key`` that must survive a retry, the ``If-Match`` lifted off
an item, the marker paging that has to stop, the part checksum, and the 302
that has to be followed by hand and streamed.

Every assertion is on a request that actually crossed the transport, or on a
byte on disk — never on a value a stub was told to hand back.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from alkera_sdk import AlkeraClient
from alkera_sdk.client import (
    MAX_RETRY_AFTER_SECONDS,
    AlkeraAuthError,
    AlkeraHTTPError,
    _FilesApi,
)
from blake3 import blake3


class Recorder:
    """A transport that records every request and replays a scripted answer."""

    def __init__(self, answers: dict[tuple[str, str], list[httpx.Response]] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._answers = answers or {}
        self.default = httpx.Response(200, json={"ok": True})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        queued = self._answers.get((request.method, request.url.path))
        if not queued:
            return self.default
        return queued.pop(0) if len(queued) > 1 else queued[0]

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def paths(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


def client(recorder: Recorder) -> AlkeraClient:
    return AlkeraClient(
        base_url="http://test", httpx_args={"transport": httpx.MockTransport(recorder)}
    )


DRIVE = "d1"
ITEM = "n1"
ITEM_ROW: dict[str, Any] = {"id": ITEM, "name": "notes.txt", "etag": 7}


# --------------------------------------------------------------------------
# every verb reaches for the right path + method
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        pytest.param(lambda f: f.drive(), ("GET", "/api/v1/files/drives"), id="drive"),
        pytest.param(
            lambda f: f.item(DRIVE, ITEM),
            ("GET", "/api/v1/files/drives/d1/items/n1"),
            id="item",
        ),
        pytest.param(
            lambda f: f.item_by_path(DRIVE, "/papers/a.txt"),
            ("GET", "/api/v1/files/drives/d1/root:/papers/a.txt"),
            id="item-by-path",
        ),
        pytest.param(
            lambda f: f.create_folder(DRIVE, ITEM, "papers"),
            ("POST", "/api/v1/files/drives/d1/items/n1/children"),
            id="create-folder",
        ),
        pytest.param(
            lambda f: f.create_tree(DRIVE, ITEM, ["a", "a/b"]),
            ("POST", "/api/v1/files/drives/d1/items/n1/tree"),
            id="create-tree",
        ),
        pytest.param(
            lambda f: f.rename(DRIVE, ITEM, "new.txt", if_match=ITEM_ROW),
            ("PATCH", "/api/v1/files/drives/d1/items/n1"),
            id="rename",
        ),
        pytest.param(
            lambda f: f.move(DRIVE, ITEM, "p2", if_match=ITEM_ROW),
            ("PATCH", "/api/v1/files/drives/d1/items/n1"),
            id="move",
        ),
        pytest.param(
            lambda f: f.trash(DRIVE, ITEM, if_match=ITEM_ROW),
            ("DELETE", "/api/v1/files/drives/d1/items/n1"),
            id="trash",
        ),
        pytest.param(
            lambda f: f.restore(DRIVE, "op9"),
            ("POST", "/api/v1/files/drives/d1/trash/op9/restore"),
            id="restore",
        ),
        pytest.param(
            lambda f: f.list_trash(DRIVE), ("GET", "/api/v1/files/drives/d1/trash"), id="list-trash"
        ),
        pytest.param(
            lambda f: f.star(DRIVE, ITEM, if_match=ITEM_ROW),
            ("PUT", "/api/v1/files/drives/d1/items/n1/star"),
            id="star",
        ),
        pytest.param(
            lambda f: f.unstar(DRIVE, ITEM, if_match=ITEM_ROW),
            ("DELETE", "/api/v1/files/drives/d1/items/n1/star"),
            id="unstar",
        ),
        pytest.param(
            lambda f: f.permissions(DRIVE, ITEM),
            ("GET", "/api/v1/files/drives/d1/items/n1/permissions"),
            id="permissions",
        ),
        pytest.param(
            lambda f: f.grant_permission(
                DRIVE,
                ITEM,
                principal={"kind": "user", "id": "u1"},
                role="reader",
                if_match=ITEM_ROW,
            ),
            ("POST", "/api/v1/files/drives/d1/items/n1/permissions"),
            id="grant",
        ),
        pytest.param(
            lambda f: f.revoke_permission(DRIVE, ITEM, "s1", if_match=ITEM_ROW),
            ("DELETE", "/api/v1/files/drives/d1/items/n1/permissions/s1"),
            id="revoke",
        ),
        pytest.param(
            lambda f: f.acquire_lease(
                DRIVE, ITEM, instance_id="i1", machine_id="m1", if_match=ITEM_ROW
            ),
            ("POST", "/api/v1/files/drives/d1/items/n1/lease"),
            id="lease-acquire",
        ),
        pytest.param(
            lambda f: f.heartbeat_lease(DRIVE, ITEM, epoch=3, instance_id="i1"),
            ("POST", "/api/v1/files/drives/d1/items/n1/lease/heartbeat"),
            id="lease-heartbeat",
        ),
        pytest.param(
            lambda f: f.release_lease(DRIVE, ITEM, epoch=3, instance_id="i1", if_match=ITEM_ROW),
            ("POST", "/api/v1/files/drives/d1/items/n1/lease/release"),
            id="lease-release",
        ),
        pytest.param(
            lambda f: f.push_snapshot(
                DRIVE, ITEM, epoch=3, instance_id="i1", changes=[], if_match=ITEM_ROW
            ),
            ("POST", "/api/v1/files/drives/d1/items/n1/snapshots"),
            id="lease-snapshot",
        ),
        pytest.param(
            lambda f: f.request_release(DRIVE, ITEM, if_match=ITEM_ROW),
            ("POST", "/api/v1/files/drives/d1/items/n1/lease/request-release"),
            id="lease-request",
        ),
        pytest.param(
            lambda f: f.force_release(DRIVE, ITEM, if_match=ITEM_ROW),
            ("POST", "/api/v1/files/drives/d1/items/n1/lease/force-release"),
            id="lease-force",
        ),
        pytest.param(
            lambda f: f.my_leases(DRIVE),
            ("GET", "/api/v1/files/drives/d1/leases"),
            id="my-leases",
        ),
        pytest.param(
            lambda f: f.operation(DRIVE, "op1"),
            ("GET", "/api/v1/files/drives/d1/operations/op1"),
            id="operation",
        ),
        pytest.param(
            lambda f: f.cancel_operation(DRIVE, "op1"),
            ("POST", "/api/v1/files/drives/d1/operations/op1/cancel"),
            id="operation-cancel",
        ),
        pytest.param(
            lambda f: f.undo_operation(DRIVE, "op1"),
            ("POST", "/api/v1/files/drives/d1/operations/op1/undo"),
            id="operation-undo",
        ),
        pytest.param(
            lambda f: f.open_upload(parent_id="p1", name="a.bin", declared_size=1),
            ("POST", "/api/v1/files/uploads"),
            id="upload-open",
        ),
        pytest.param(
            lambda f: f.put_part("s1", 2, b"x", checksum="ab"),
            ("PUT", "/api/v1/files/uploads/s1/parts/2"),
            id="upload-part",
        ),
        pytest.param(
            lambda f: f.complete_upload("s1", []),
            ("POST", "/api/v1/files/uploads/s1/complete"),
            id="upload-complete",
        ),
        pytest.param(
            lambda f: f.upload_status("s1"), ("GET", "/api/v1/files/uploads/s1"), id="upload-status"
        ),
        pytest.param(
            lambda f: f.abort_upload("s1"),
            ("DELETE", "/api/v1/files/uploads/s1"),
            id="upload-abort",
        ),
    ],
)
def test_every_verb_reaches_its_own_route(call: Any, expected: tuple[str, str]) -> None:
    # The tree route is the one whose success body is an array rather than an
    # object, so the stub has to answer it in its own shape.
    recorder = Recorder(
        {
            ("POST", "/api/v1/files/drives/d1/items/n1/tree"): [httpx.Response(201, json=[])],
            # The two listings whose success body is an array rather than an
            # object have to be answered in their own shape.
            ("GET", "/api/v1/files/drives/d1/leases"): [httpx.Response(200, json=[])],
        }
    )
    with client(recorder) as api:
        call(api.files)
    assert (recorder.last.method, recorder.last.url.path) == expected


# --------------------------------------------------------------------------
# headers: Idempotency-Key, If-Match, the lease fence
# --------------------------------------------------------------------------


def test_a_mutation_carries_an_idempotency_key_and_a_get_does_not() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        api.files.create_folder(DRIVE, ITEM, "papers")
        api.files.item(DRIVE, ITEM)
    post, get = recorder.requests
    assert "Idempotency-Key" in post.headers
    assert "Idempotency-Key" not in get.headers


def test_two_calls_mint_two_different_keys() -> None:
    """The key identifies one call, not one client — two folders are two keys."""
    recorder = Recorder()
    with client(recorder) as api:
        api.files.create_folder(DRIVE, ITEM, "a")
        api.files.create_folder(DRIVE, ITEM, "b")
    first, second = (r.headers["Idempotency-Key"] for r in recorder.requests)
    assert first != second


def test_a_retried_call_replays_the_same_key() -> None:
    """A 503 is retried with the key the first attempt minted — replaying the
    first answer is the entire point of the header.
    """
    recorder = Recorder(
        {
            ("POST", "/api/v1/files/uploads/s1/complete"): [
                httpx.Response(503, json={"code": "unavailable"}),
                httpx.Response(202, json={"id": "op1", "state": "running"}),
            ]
        }
    )
    with client(recorder) as api:
        result = api.files.complete_upload("s1", [{"partNo": 1, "size": 4, "checksum": "aa"}])
    assert result["id"] == "op1"
    assert len(recorder.requests) == 2
    keys = {r.headers["Idempotency-Key"] for r in recorder.requests}
    assert len(keys) == 1


def test_a_transport_error_is_retried_with_the_same_key() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Idempotency-Key"])
        if len(seen) == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    with AlkeraClient(
        base_url="http://test", httpx_args={"transport": httpx.MockTransport(handler)}
    ) as api:
        api.files.restore(DRIVE, "op9")
    assert len(seen) == 2
    assert seen[0] == seen[1]


def _retrying_files(recorder: Recorder, waits: list[float]) -> _FilesApi:
    """The namespace on a scripted transport, with the pause recorded not taken."""
    return _FilesApi(
        httpx.Client(base_url="http://test", transport=httpx.MockTransport(recorder)),
        sleep=waits.append,
    )


def test_a_refusal_that_names_a_pause_is_obeyed_before_the_replay() -> None:
    """A busy database answers ``503`` with the seconds it needs.

    Replaying inside that window is one more request it is in no state to
    answer, and every client doing it is how a recoverable stall stays one.
    """
    recorder = Recorder(
        {
            ("POST", "/api/v1/files/drives/d1/trash/op9/restore"): [
                httpx.Response(
                    503,
                    headers={"Retry-After": "2"},
                    json={"error": {"code": "db_lock_timeout"}},
                ),
                httpx.Response(200, json={"ok": True}),
            ]
        }
    )
    waits: list[float] = []

    _retrying_files(recorder, waits).restore(DRIVE, "op9")

    assert waits == [2.0]
    assert len(recorder.requests) == 2
    assert len({r.headers["Idempotency-Key"] for r in recorder.requests}) == 1


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param(None, [], id="none-named-none-taken"),
        pytest.param("0", [], id="zero-is-no-pause"),
        pytest.param("-5", [], id="a-negative-pause-is-no-pause"),
        pytest.param("soon", [], id="an-unparseable-pause-is-no-pause"),
        pytest.param(" 1.5 ", [1.5], id="whitespace-and-fractions-are-read"),
        pytest.param("9999", [MAX_RETRY_AFTER_SECONDS], id="a-huge-pause-is-clamped"),
    ],
)
def test_the_pause_a_server_names_is_read_and_bounded(
    header: str | None, expected: list[float]
) -> None:
    """The header comes from the far side, so every shape of it is answered
    here rather than by a thread that stops for as long as it is told."""
    recorder = Recorder(
        {
            ("POST", "/api/v1/files/drives/d1/trash/op9/restore"): [
                httpx.Response(503, headers={} if header is None else {"Retry-After": header}),
                httpx.Response(200, json={"ok": True}),
            ]
        }
    )
    waits: list[float] = []

    _retrying_files(recorder, waits).restore(DRIVE, "op9")

    assert waits == expected
    assert len(recorder.requests) == 2


def test_retries_are_bounded_and_the_last_failure_surfaces() -> None:
    recorder = Recorder(
        {("POST", "/api/v1/files/drives/d1/trash/op9/restore"): [httpx.Response(503, text="down")]}
    )
    with client(recorder) as api, pytest.raises(RuntimeError, match="503"):
        api.files.restore(DRIVE, "op9")
    assert len(recorder.requests) == 3


def test_if_match_is_lifted_off_the_item() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        api.files.rename(DRIVE, ITEM, "new.txt", if_match=ITEM_ROW)
    assert recorder.last.headers["If-Match"] == "7"


def test_if_match_accepts_a_bare_etag() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        api.files.trash(DRIVE, ITEM, if_match="12")
    assert recorder.last.headers["If-Match"] == "12"


def test_an_item_with_no_etag_refuses_to_mutate() -> None:
    """Sending no If-Match would be a 428 round-trip; refusing locally names
    the real problem — this item was fetched without its etag.
    """
    recorder = Recorder()
    with client(recorder) as api, pytest.raises(ValueError, match="etag"):
        api.files.trash(DRIVE, ITEM, if_match={"id": ITEM})
    assert recorder.requests == []


def test_a_fenced_write_carries_the_lease_instance_and_epoch() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        api.files.push_snapshot(
            DRIVE, ITEM, epoch=4, instance_id="L7", changes=[{"path": "a"}], if_match=ITEM_ROW
        )
    assert recorder.last.headers["X-Alkera-Lease-Instance"] == "L7"
    assert recorder.last.headers["X-Alkera-Lease-Epoch"] == "4"


# --------------------------------------------------------------------------
# bodies
# --------------------------------------------------------------------------


def test_create_folder_sends_the_folder_kind_and_conflict_behavior() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        api.files.create_folder(DRIVE, ITEM, "papers", conflict_behavior="rename")
    body = json.loads(recorder.last.content)
    assert body == {"kind": "folder", "name": "papers", "conflictBehavior": "rename"}


def test_move_sends_only_the_parent_when_no_name_is_given() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        api.files.move(DRIVE, ITEM, "p2", if_match=ITEM_ROW)
    assert json.loads(recorder.last.content) == {"parentId": "p2"}


def test_rename_puts_the_conflict_behavior_in_the_query_not_the_body() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        api.files.rename(DRIVE, ITEM, "new.txt", if_match=ITEM_ROW, conflict_behavior="rename")
    assert json.loads(recorder.last.content) == {"name": "new.txt"}
    assert recorder.last.url.params["conflict_behavior"] == "rename"


# --------------------------------------------------------------------------
# paging
# --------------------------------------------------------------------------


def test_children_follows_the_marker_and_stops_when_it_is_absent() -> None:
    pages = [
        httpx.Response(200, json={"value": [{"id": "a"}], "nextMarker": "m1"}),
        httpx.Response(200, json={"value": [{"id": "b"}], "nextMarker": "m2"}),
        httpx.Response(200, json={"value": [{"id": "c"}]}),
    ]

    index = [0]
    markers: list[str | None] = []

    def paging(request: httpx.Request) -> httpx.Response:
        response = pages[index[0]]
        index[0] += 1
        markers.append(request.url.params.get("marker"))
        return response

    with AlkeraClient(
        base_url="http://test", httpx_args={"transport": httpx.MockTransport(paging)}
    ) as api:
        rows = list(api.files.children(DRIVE, ITEM))
    assert [r["id"] for r in rows] == ["a", "b", "c"]
    assert markers == [None, "m1", "m2"]


def test_children_stops_on_the_first_page_when_there_is_no_marker() -> None:
    recorder = Recorder(
        {
            ("GET", "/api/v1/files/drives/d1/items/n1/children"): [
                httpx.Response(200, json={"value": [{"id": "a"}]})
            ]
        }
    )
    with client(recorder) as api:
        rows = list(api.files.children(DRIVE, ITEM))
    assert [r["id"] for r in rows] == ["a"]
    assert len(recorder.requests) == 1


def test_children_refuses_a_repeated_marker_instead_of_looping() -> None:
    recorder = Recorder(
        {
            ("GET", "/api/v1/files/drives/d1/items/n1/children"): [
                httpx.Response(200, json={"value": [{"id": "a"}], "nextMarker": "same"})
            ]
        }
    )
    with client(recorder) as api, pytest.raises(RuntimeError, match="repeated a paging marker"):
        list(api.files.children(DRIVE, ITEM))


def test_search_passes_its_filters_through_to_the_listing() -> None:
    recorder = Recorder(
        {("GET", "/api/v1/files/drives/d1/items/n1/children"): [httpx.Response(200, json={})]}
    )
    with client(recorder) as api:
        list(api.files.search(DRIVE, ITEM, filters={"kind": "file", "starred": "true"}))
    assert recorder.last.url.path == "/api/v1/files/drives/d1/items/n1/children"
    assert recorder.last.url.params["kind"] == "file"
    assert recorder.last.url.params["starred"] == "true"


def test_delta_follows_next_link_and_yields_the_final_page() -> None:
    pages = [
        httpx.Response(
            200, json={"value": [1], "nextLink": "/api/v1/files/drives/d1/delta?token=t2"}
        ),
        httpx.Response(200, json={"value": [2], "deltaLink": "/api/v1/files/drives/d1/delta?d=1"}),
    ]
    index = [0]
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        response = pages[index[0]]
        index[0] += 1
        return response

    with AlkeraClient(
        base_url="http://test", httpx_args={"transport": httpx.MockTransport(handler)}
    ) as api:
        got = list(api.files.delta(DRIVE, token="t1"))
    assert [page["value"] for page in got] == [[1], [2]]
    assert urls[0].endswith("token=t1")
    assert urls[1].endswith("token=t2")


# --------------------------------------------------------------------------
# uploads
# --------------------------------------------------------------------------


def _upload_transport(part_size: int, recorder: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        path = request.url.path
        if path == "/api/v1/files/uploads":
            return httpx.Response(
                201, json={"uploadId": "s1", "partSize": part_size, "partsTotal": 0}
            )
        if path.startswith("/api/v1/files/uploads/s1/parts/"):
            recorder["parts"].append(
                (
                    int(path.rsplit("/", 1)[1]),
                    request.content,
                    request.headers.get("X-Part-Checksum"),
                )
            )
            return httpx.Response(200, json={"partNo": 1, "size": len(request.content)})
        if path == "/api/v1/files/uploads/s1/complete":
            recorder["complete"] = json.loads(request.content)
            return httpx.Response(202, json={"id": "op1", "state": "running"})
        if path == "/api/v1/files/drives/d1/operations/op1":
            recorder["polls"] += 1
            state = "running" if recorder["polls"] < 3 else "done"
            return httpx.Response(200, json={"id": "op1", "state": state})
        raise AssertionError(f"unexpected {path}")

    return httpx.MockTransport(handler)


def test_upload_file_cuts_parts_at_the_servers_part_size_with_real_checksums(
    tmp_path: Any,
) -> None:
    """The part geometry comes from the server's answer, and each part's
    ``X-Part-Checksum`` is the BLAKE3 of exactly those bytes — computed here
    independently of the client's own call.
    """
    payload = bytes(range(256)) * 40  # 10 240 bytes
    source = tmp_path / "big.bin"
    source.write_bytes(payload)
    seen: dict[str, Any] = {"parts": [], "polls": 0}

    with AlkeraClient(
        base_url="http://test", httpx_args={"transport": _upload_transport(4096, seen)}
    ) as api:
        final = api.files.upload_file(source, drive_id=DRIVE, parent_id="p1", sleep=lambda _s: None)

    assert [size for _n, body, _c in seen["parts"] for size in (len(body),)] == [4096, 4096, 2048]
    assert [n for n, _b, _c in seen["parts"]] == [1, 2, 3]
    assert b"".join(body for _n, body, _c in seen["parts"]) == payload
    for _n, body, checksum in seen["parts"]:
        assert checksum == blake3(body).hexdigest()
    assert seen["complete"]["parts"] == [
        {"partNo": 1, "size": 4096, "checksum": blake3(payload[:4096]).hexdigest()},
        {"partNo": 2, "size": 4096, "checksum": blake3(payload[4096:8192]).hexdigest()},
        {"partNo": 3, "size": 2048, "checksum": blake3(payload[8192:]).hexdigest()},
    ]
    assert final["state"] == "done"


def test_upload_file_declares_the_real_size_and_the_file_name(tmp_path: Any) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"hello")
    opened: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        if request.url.path == "/api/v1/files/uploads":
            opened.append(json.loads(request.content))
            return httpx.Response(201, json={"uploadId": "s1", "partSize": 64})
        if request.url.path.endswith("/complete"):
            return httpx.Response(202, json={"id": "op1", "state": "done"})
        return httpx.Response(200, json={"id": "op1", "state": "done"})

    with AlkeraClient(
        base_url="http://test", httpx_args={"transport": httpx.MockTransport(handler)}
    ) as api:
        api.files.upload_file(source, drive_id=DRIVE, parent_id="p1", wait=False)
    assert opened == [{"parentId": "p1", "name": "notes.txt", "declaredSize": 5}]


def test_upload_file_can_skip_the_wait_and_hand_back_the_operation(tmp_path: Any) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"abc")
    seen: dict[str, Any] = {"parts": [], "polls": 0}
    with AlkeraClient(
        base_url="http://test", httpx_args={"transport": _upload_transport(64, seen)}
    ) as api:
        operation = api.files.upload_file(source, drive_id=DRIVE, parent_id="p1", wait=False)
    assert operation["state"] == "running"
    assert seen["polls"] == 0


def test_await_operation_gives_up_at_its_deadline() -> None:
    recorder = Recorder(
        {
            ("GET", "/api/v1/files/drives/d1/operations/op1"): [
                httpx.Response(200, json={"id": "op1", "state": "running"})
            ]
        }
    )
    ticks = iter([0.0, 0.0, 10.0])
    with client(recorder) as api, pytest.raises(TimeoutError, match="op1"):
        api.files.await_operation(
            DRIVE, "op1", timeout=5.0, sleep=lambda _s: None, now=lambda: next(ticks)
        )


def test_await_operation_backs_off_and_polls_a_slow_operation_a_bounded_number_of_times() -> None:
    """A commit the server is slow on is not asked about five times a second
    for the whole wait: the gap doubles to a ceiling, so a minute's wait is a
    few dozen polls rather than three hundred."""
    recorder = Recorder(
        {
            ("GET", "/api/v1/files/drives/d1/operations/op1"): [
                httpx.Response(200, json={"id": "op1", "state": "running"})
            ]
        }
    )
    clock = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    with client(recorder) as api, pytest.raises(TimeoutError, match="op1"):
        api.files.await_operation(
            DRIVE, "op1", timeout=60.0, interval=0.2, sleep=sleep, now=lambda: clock[0]
        )
    assert slept[:5] == [0.2, 0.4, 0.8, 1.6, 2.0]
    assert max(slept) == 2.0
    assert len(slept) + 1 <= 35, f"{len(slept) + 1} polls in a minute"
    assert clock[0] == pytest.approx(60.0)


def test_await_operation_answers_a_quick_operation_on_the_first_short_wait() -> None:
    recorder = Recorder(
        {
            ("GET", "/api/v1/files/drives/d1/operations/op1"): [
                httpx.Response(200, json={"id": "op1", "state": "running"}),
                httpx.Response(200, json={"id": "op1", "state": "done"}),
            ]
        }
    )
    slept: list[float] = []
    with client(recorder) as api:
        state = api.files.await_operation(
            DRIVE, "op1", timeout=60.0, interval=0.1, sleep=slept.append, now=lambda: 0.0
        )
    assert state["state"] == "done"
    assert slept == [0.1]


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------


def test_download_follows_the_redirect_once_and_streams_the_bytes(tmp_path: Any) -> None:
    payload = b"the signed content" * 100
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "http://content.test/signed?n=1"})
        return httpx.Response(200, content=payload)

    dest = tmp_path / "out.bin"
    with AlkeraClient(
        base_url="http://test", httpx_args={"transport": httpx.MockTransport(handler)}
    ) as api:
        written = api.files.download(DRIVE, ITEM, dest)

    assert written.read_bytes() == payload
    assert hits == [
        "http://test/api/v1/files/drives/d1/items/n1/content",
        "http://content.test/signed?n=1",
    ]


def test_download_refuses_a_non_redirect_answer(tmp_path: Any) -> None:
    recorder = Recorder(
        {("GET", "/api/v1/files/drives/d1/items/n1/content"): [httpx.Response(200, text="oops")]}
    )
    with client(recorder) as api, pytest.raises(RuntimeError, match="files/download"):
        api.files.download(DRIVE, ITEM, tmp_path / "out.bin")
    assert not (tmp_path / "out.bin").exists()


def test_download_reports_a_failure_at_the_signed_url(tmp_path: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "http://content.test/signed"})
        return httpx.Response(403, text="expired")

    with (
        AlkeraClient(
            base_url="http://test", httpx_args={"transport": httpx.MockTransport(handler)}
        ) as api,
        pytest.raises(RuntimeError, match="403"),
    ):
        api.files.download(DRIVE, ITEM, tmp_path / "out.bin")


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


def test_a_404_raises_with_the_route_label_and_the_body() -> None:
    recorder = Recorder(
        {
            ("GET", "/api/v1/files/drives/d1/items/n1"): [
                httpx.Response(404, json={"code": "not_found"})
            ]
        }
    )
    with client(recorder) as api, pytest.raises(RuntimeError, match="files/item returned 404"):
        api.files.item(DRIVE, ITEM)


# --------------------------------------------------------------------------
# the shapes the routes really answer with: a list, a 202, a 200-vs-201
# --------------------------------------------------------------------------


def test_create_tree_returns_the_list_the_route_answered_with() -> None:
    """``POST …/tree`` answers 201 with a JSON *array* of the folders it made.

    Putting that through the object-only reader turned every success into a
    raised error, so this is the whole point of the list reader.
    """
    made = [{"id": "f1", "name": "papers"}, {"id": "f2", "name": "img"}]
    recorder = Recorder(
        {("POST", "/api/v1/files/drives/d1/items/n1/tree"): [httpx.Response(201, json=made)]}
    )
    with client(recorder) as api:
        created = api.files.create_tree(DRIVE, ITEM, ["papers", "papers/img"])
    assert created == made
    assert json.loads(recorder.last.content) == {"paths": ["papers", "papers/img"]}


def test_create_tree_accepts_the_empty_list_a_re_drop_answers_with() -> None:
    """Every path already existed, so nothing was created — still a success."""
    recorder = Recorder(
        {("POST", "/api/v1/files/drives/d1/items/n1/tree"): [httpx.Response(201, json=[])]}
    )
    with client(recorder) as api:
        assert api.files.create_tree(DRIVE, ITEM, ["papers"]) == []


def test_create_tree_refuses_a_body_that_is_not_a_list() -> None:
    recorder = Recorder(
        {("POST", "/api/v1/files/drives/d1/items/n1/tree"): [httpx.Response(201, json={"id": "f"})]}
    )
    with client(recorder) as api, pytest.raises(RuntimeError, match="files/create-tree"):
        api.files.create_tree(DRIVE, ITEM, ["papers"])


def test_create_child_sends_the_symlink_kind_and_its_target() -> None:
    recorder = Recorder(
        {
            ("POST", "/api/v1/files/drives/d1/items/n1/children"): [
                httpx.Response(201, json={"id": "s1", "kind": "symlink"})
            ]
        }
    )
    with client(recorder) as api:
        made = api.files.create_child(
            DRIVE, ITEM, "link", kind="symlink", symlink_target="../real.txt"
        )
    assert made["kind"] == "symlink"
    assert json.loads(recorder.last.content) == {
        "kind": "symlink",
        "name": "link",
        "conflictBehavior": "fail",
        "symlinkTarget": "../real.txt",
    }


def test_create_child_sends_a_specials_subtype_and_omits_what_it_was_not_given() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        api.files.create_child(DRIVE, ITEM, "dev", kind="special", subtype="fifo")
    body = json.loads(recorder.last.content)
    assert body["kind"] == "special"
    assert body["subtype"] == "fifo"
    assert "symlinkTarget" not in body


def test_patch_item_sends_a_rename_a_move_and_attrs_in_one_body() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        api.files.patch_item(
            DRIVE, ITEM, if_match=ITEM_ROW, name="new.txt", parent_id="p2", attrs={"mode": 420}
        )
    assert json.loads(recorder.last.content) == {
        "name": "new.txt",
        "parentId": "p2",
        "attrs": {"mode": 420},
    }
    assert recorder.last.headers["If-Match"] == "7"
    assert "conflict_behavior" not in recorder.last.url.params


def test_patch_item_refuses_a_call_that_would_change_nothing() -> None:
    recorder = Recorder()
    with client(recorder) as api, pytest.raises(ValueError, match="nothing to change"):
        api.files.patch_item(DRIVE, ITEM, if_match=ITEM_ROW)
    assert recorder.requests == []


def test_a_move_too_large_to_run_inline_hands_back_its_202_operation() -> None:
    """The route answers 202 with the operation instead of the node.

    Treating anything but 200 as a failure here would make the large-move path
    unreachable from the SDK, so the operation has to come back as the answer.
    """
    operation = {"id": "op1", "state": "running", "done": 0, "total": 40000}
    recorder = Recorder(
        {("PATCH", "/api/v1/files/drives/d1/items/n1"): [httpx.Response(202, json=operation)]}
    )
    with client(recorder) as api:
        answered = api.files.move(DRIVE, ITEM, "p2", if_match=ITEM_ROW)
    assert answered == operation


def test_put_content_puts_the_bytes_on_the_node_with_its_conflict_mode() -> None:
    recorder = Recorder(
        {
            ("PUT", "/api/v1/files/drives/d1/items/n1/content"): [
                httpx.Response(201, json={"id": "n1", "file": {"size": 5}})
            ]
        }
    )
    with client(recorder) as api:
        written = api.files.put_content(DRIVE, ITEM, b"hello", if_match=ITEM_ROW, mime="text/plain")
    assert written["file"]["size"] == 5
    assert recorder.last.content == b"hello"
    assert recorder.last.url.params["conflictBehavior"] == "replace"
    assert recorder.last.headers["Content-Type"] == "text/plain"
    assert recorder.last.headers["If-Match"] == "7"
    assert "Idempotency-Key" in recorder.last.headers


def test_put_content_treats_the_identical_bytes_200_as_a_success() -> None:
    """The head already held these bytes: no new version, still the item."""
    recorder = Recorder(
        {
            ("PUT", "/api/v1/files/drives/d1/items/n1/content"): [
                httpx.Response(200, json={"id": "n1", "etag": 7})
            ]
        }
    )
    with client(recorder) as api:
        assert api.files.put_content(DRIVE, ITEM, b"hello", if_match=ITEM_ROW)["etag"] == 7


# --------------------------------------------------------------------------
# the feeds — their own routes, paged like every other listing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "expected_path"),
    [
        pytest.param(
            lambda f: list(f.search_names(DRIVE, q="notes")),
            "/api/v1/files/drives/d1/search",
            id="search-names",
        ),
        pytest.param(
            lambda f: list(f.recent(DRIVE)), "/api/v1/files/drives/d1/recent", id="recent"
        ),
        pytest.param(
            lambda f: list(f.starred(DRIVE)), "/api/v1/files/drives/d1/starred", id="starred"
        ),
        pytest.param(
            lambda f: list(f.shared_with_me(DRIVE)),
            "/api/v1/files/drives/d1/sharedWithMe",
            id="shared-with-me",
        ),
        pytest.param(
            lambda f: list(f.activity(DRIVE, ITEM)),
            "/api/v1/files/drives/d1/items/n1/activity",
            id="activity",
        ),
        pytest.param(
            lambda f: f.bulk(DRIVE, [{"id": "a", "op": "delete", "itemId": "n1"}]),
            "/api/v1/files/drives/d1/bulk",
            id="bulk",
        ),
        pytest.param(
            lambda f: f.copy(DRIVE, ITEM, "p2"), "/api/v1/files/drives/d1/items/n1/copy", id="copy"
        ),
        pytest.param(
            lambda f: f.download_subtree(DRIVE, ITEM, if_match=ITEM_ROW),
            "/api/v1/files/drives/d1/items/n1/download",
            id="download-subtree",
        ),
        pytest.param(
            lambda f: f.versions(DRIVE, ITEM),
            "/api/v1/files/drives/d1/items/n1/versions",
            id="versions",
        ),
        pytest.param(
            lambda f: f.restore_version(DRIVE, ITEM, "v3", if_match=ITEM_ROW),
            "/api/v1/files/drives/d1/items/n1/versions/v3/restore",
            id="version-restore",
        ),
        pytest.param(
            lambda f: f.empty_trash(DRIVE), "/api/v1/files/drives/d1/trash/empty", id="trash-empty"
        ),
        pytest.param(
            lambda f: f.conflicts(DRIVE), "/api/v1/files/drives/d1/conflicts", id="conflicts"
        ),
        pytest.param(
            lambda f: f.resolve_conflict(DRIVE, "c1", keep="mine"),
            "/api/v1/files/drives/d1/conflicts/c1/resolve",
            id="conflict-resolve",
        ),
    ],
)
def test_the_completed_verbs_reach_their_own_routes(call: Any, expected_path: str) -> None:
    recorder = Recorder()
    with client(recorder) as api:
        call(api.files)
    assert recorder.last.url.path == expected_path


def test_search_names_scopes_to_a_folder_and_carries_its_query_and_chips() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        list(
            api.files.search_names(
                DRIVE, q="budget", scope_item_id=ITEM, filters={"kind": "file"}, limit=25
            )
        )
    params = recorder.last.url.params
    assert recorder.last.url.path == "/api/v1/files/drives/d1/search"
    assert params["q"] == "budget"
    assert params["scope"] == "folder:n1"
    assert params["kind"] == "file"
    assert params["limit"] == "25"


def test_search_names_without_a_folder_covers_the_whole_drive() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        list(api.files.search_names(DRIVE, q="budget"))
    assert recorder.last.url.params["scope"] == "drive"


def test_activity_sends_the_subtree_switch_only_when_it_is_asked_for() -> None:
    """The route reads the literal ``"true"``, and any value at all would read
    as present — so a ``False`` must not reach the wire at all."""
    recorder = Recorder()
    with client(recorder) as api:
        list(api.files.activity(DRIVE, ITEM))
        assert "ancestor" not in recorder.last.url.params
        list(api.files.activity(DRIVE, ITEM, ancestor=True))
    assert recorder.last.url.params["ancestor"] == "true"


def test_a_feed_follows_its_marker_across_pages_and_stops() -> None:
    pages = [
        httpx.Response(200, json={"value": [{"id": "a"}], "nextMarker": "m2"}),
        httpx.Response(200, json={"value": [{"id": "b"}]}),
    ]
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("marker"))
        return pages[len(seen) - 1]

    with AlkeraClient(
        base_url="http://test", httpx_args={"transport": httpx.MockTransport(handler)}
    ) as api:
        rows = list(api.files.recent(DRIVE))
    assert [row["id"] for row in rows] == ["a", "b"]
    assert seen == [None, "m2"]


def test_a_feed_refuses_a_repeated_marker_instead_of_looping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [], "nextMarker": "same"})

    with (
        AlkeraClient(
            base_url="http://test", httpx_args={"transport": httpx.MockTransport(handler)}
        ) as api,
        pytest.raises(RuntimeError, match="files/starred repeated a paging marker"),
    ):
        list(api.files.starred(DRIVE))


# --------------------------------------------------------------------------
# bulk, copy, download, permissions, versions — bodies and statuses
# --------------------------------------------------------------------------


def test_bulk_sends_the_batch_and_hands_back_the_per_item_rows() -> None:
    answer = {"responses": [{"id": "a", "status": 204}, {"id": "b", "status": 404}]}
    recorder = Recorder(
        {("POST", "/api/v1/files/drives/d1/bulk"): [httpx.Response(200, json=answer)]}
    )
    items = [
        {"id": "a", "op": "delete", "itemId": "n1", "ifMatch": 7},
        {"id": "b", "op": "delete", "itemId": "n2"},
    ]
    with client(recorder) as api:
        assert api.files.bulk(DRIVE, items)["responses"][1]["status"] == 404
    assert json.loads(recorder.last.content) == {"items": items}


def test_a_batch_too_large_to_run_inline_hands_back_its_202_operation() -> None:
    recorder = Recorder(
        {
            ("POST", "/api/v1/files/drives/d1/bulk"): [
                httpx.Response(202, json={"id": "op9", "state": "running"})
            ]
        }
    )
    with client(recorder) as api:
        assert api.files.bulk(DRIVE, [{"id": "a", "op": "delete"}])["state"] == "running"


def test_copy_names_its_destination_and_returns_the_202_operation() -> None:
    recorder = Recorder(
        {
            ("POST", "/api/v1/files/drives/d1/items/n1/copy"): [
                httpx.Response(202, json={"id": "op2", "state": "running"})
            ]
        }
    )
    with client(recorder) as api:
        operation = api.files.copy(DRIVE, ITEM, "p2", name="copy.txt")
    assert operation["id"] == "op2"
    assert json.loads(recorder.last.content) == {
        "parentId": "p2",
        "conflictBehavior": "rename",
        "name": "copy.txt",
    }


def test_download_subtree_carries_the_if_match_the_route_requires() -> None:
    recorder = Recorder(
        {
            ("POST", "/api/v1/files/drives/d1/items/n1/download"): [
                httpx.Response(202, json={"id": "op3", "state": "done", "resultUrl": "/a.zip"})
            ]
        }
    )
    with client(recorder) as api:
        operation = api.files.download_subtree(DRIVE, ITEM, if_match=ITEM_ROW)
    assert operation["resultUrl"] == "/a.zip"
    assert recorder.last.headers["If-Match"] == "7"


def test_permissions_asks_for_the_effective_set_only_when_told_to() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        api.files.permissions(DRIVE, ITEM)
        assert "effective" not in recorder.last.url.params
        api.files.permissions(DRIVE, ITEM, effective=True)
    assert recorder.last.url.params["effective"] == "true"


def test_restore_version_carries_the_etag_and_a_fresh_idempotency_key() -> None:
    recorder = Recorder()
    with client(recorder) as api:
        api.files.restore_version(DRIVE, ITEM, "v3", if_match="9")
    assert recorder.last.headers["If-Match"] == "9"
    assert "Idempotency-Key" in recorder.last.headers


def test_download_version_follows_its_redirect_once_and_streams_the_bytes(tmp_path: Any) -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(request.url.path)
        if request.url.path.endswith("/versions/v3/content"):
            return httpx.Response(302, headers={"location": "http://content/blob/v3"})
        return httpx.Response(200, content=b"older bytes")

    with AlkeraClient(
        base_url="http://test", httpx_args={"transport": httpx.MockTransport(handler)}
    ) as api:
        out = api.files.download_version(DRIVE, ITEM, "v3", tmp_path / "old.bin")
    assert out.read_bytes() == b"older bytes"
    assert hops == ["/api/v1/files/drives/d1/items/n1/versions/v3/content", "/blob/v3"]


# --------------------------------------------------------------------------
# every completed verb maps its refusals the same way
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "label"),
    [
        pytest.param(lambda f: f.create_tree(DRIVE, ITEM, ["a"]), "files/create-tree", id="tree"),
        pytest.param(
            lambda f: f.create_child(DRIVE, ITEM, "a", kind="special"),
            "files/create-child",
            id="child",
        ),
        pytest.param(
            lambda f: f.put_content(DRIVE, ITEM, b"x", if_match="1"), "files/put-content", id="put"
        ),
        pytest.param(
            lambda f: list(f.search_names(DRIVE, q="a")), "files/search", id="search-names"
        ),
        pytest.param(lambda f: list(f.recent(DRIVE)), "files/recent", id="recent"),
        pytest.param(
            lambda f: f.bulk(DRIVE, [{"id": "a", "op": "delete"}]), "files/bulk", id="bulk"
        ),
        pytest.param(lambda f: f.copy(DRIVE, ITEM, "p2"), "files/copy", id="copy"),
        pytest.param(lambda f: f.versions(DRIVE, ITEM), "files/versions", id="versions"),
    ],
)
def test_a_refused_call_raises_the_typed_error_naming_its_route(call: Any, label: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "not_found"})

    with (
        AlkeraClient(
            base_url="http://test", httpx_args={"transport": httpx.MockTransport(handler)}
        ) as api,
        pytest.raises(AlkeraHTTPError) as raised,
    ):
        call(api.files)
    assert raised.value.status == 404
    assert raised.value.code == "not_found"
    assert label in str(raised.value)
    assert not isinstance(raised.value, AlkeraAuthError)


@pytest.mark.parametrize("refusal", [401, 403])
def test_a_rejected_credential_raises_the_auth_error_on_a_completed_verb(refusal: int) -> None:
    """401 and 403 are the one refusal a caller answers by signing in again."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(refusal, json={"code": "forbidden"})

    with (
        AlkeraClient(
            base_url="http://test", httpx_args={"transport": httpx.MockTransport(handler)}
        ) as api,
        pytest.raises(AlkeraAuthError) as raised,
    ):
        api.files.put_content(DRIVE, ITEM, b"x", if_match="1")
    assert raised.value.status == refusal


# --------------------------------------------------------------------------
# the lease family, against the shapes the routes actually take
# --------------------------------------------------------------------------


def _body(request: httpx.Request) -> dict[str, Any]:
    parsed: Any = json.loads(request.content) if request.content else {}
    assert isinstance(parsed, dict)
    return parsed


def test_acquire_names_the_instance_and_the_machine() -> None:
    """``POST …/lease`` takes ``{instanceId, machineId, purpose, ttl}``.

    Both ids are the point: the instance is what the server fences writes on,
    and without it an acquire is a 422 no client can recover from.
    """
    recorder = Recorder()
    with client(recorder) as api:
        api.files.acquire_lease(
            DRIVE,
            ITEM,
            instance_id="laptop-7",
            machine_id="MacBook Pro",
            ttl=120,
            if_match=ITEM_ROW,
        )
    assert _body(recorder.last) == {
        "instanceId": "laptop-7",
        "machineId": "MacBook Pro",
        "purpose": "mount",
        "ttl": 120,
    }
    assert recorder.last.headers["If-Match"] == "7"
    assert "Idempotency-Key" in recorder.last.headers


def test_acquire_omits_the_ttl_it_was_not_given() -> None:
    """No ``ttl`` means the server's own default, not a null the model rejects."""
    recorder = Recorder()
    with client(recorder) as api:
        api.files.acquire_lease(DRIVE, ITEM, instance_id="i", machine_id="m", if_match=ITEM_ROW)
    assert "ttl" not in _body(recorder.last)


def test_heartbeat_sends_the_epoch_and_instance_in_the_body_and_the_fence() -> None:
    """The beat's body is ``{epoch, instanceId}`` — there is no ``leaseId``."""
    recorder = Recorder()
    with client(recorder) as api:
        api.files.heartbeat_lease(DRIVE, ITEM, epoch=9, instance_id="laptop-7")
    assert _body(recorder.last) == {"epoch": 9, "instanceId": "laptop-7"}
    assert recorder.last.headers["X-Alkera-Lease-Epoch"] == "9"
    assert recorder.last.headers["X-Alkera-Lease-Instance"] == "laptop-7"


def test_release_carries_the_final_batch_the_caller_passed() -> None:
    """An empty ``final`` is still a batch: it is sent, not dropped."""
    recorder = Recorder()
    with client(recorder) as api:
        api.files.release_lease(
            DRIVE, ITEM, epoch=9, instance_id="laptop-7", if_match=ITEM_ROW, final=[]
        )
    assert _body(recorder.last) == {"epoch": 9, "instanceId": "laptop-7", "final": []}
    assert recorder.last.headers["X-Alkera-Lease-Instance"] == "laptop-7"
    assert recorder.last.headers["If-Match"] == "7"


def test_release_without_a_final_batch_sends_no_final_key() -> None:
    """``None`` means "no final batch" to the route — an empty list does not."""
    recorder = Recorder()
    with client(recorder) as api:
        api.files.release_lease(DRIVE, ITEM, epoch=9, instance_id="i", if_match=ITEM_ROW)
    assert "final" not in _body(recorder.last)


def test_release_returns_nothing_and_does_not_choke_on_an_empty_body() -> None:
    """The route answers with no body; reading one would fail every release."""
    recorder = Recorder(
        {("POST", "/api/v1/files/drives/d1/items/n1/lease/release"): [httpx.Response(204)]}
    )
    with client(recorder) as api:
        released = api.files.release_lease(DRIVE, ITEM, epoch=1, instance_id="i", if_match=ITEM_ROW)
    assert released is None


def test_a_refused_release_still_raises() -> None:
    """A fenced release must not read as a successful hand-back."""
    recorder = Recorder(
        {
            ("POST", "/api/v1/files/drives/d1/items/n1/lease/release"): [
                httpx.Response(409, json={"code": "files.lease_fenced", "message": "gone"})
            ]
        }
    )
    with client(recorder) as api, pytest.raises(AlkeraHTTPError) as raised:
        api.files.release_lease(DRIVE, ITEM, epoch=1, instance_id="i", if_match=ITEM_ROW)
    assert raised.value.status == 409
    assert raised.value.code == "files.lease_fenced"


def test_a_snapshot_sends_changes_under_the_key_the_route_reads() -> None:
    """``SnapshotBody`` takes ``{epoch, instanceId, changes}`` — an ``entries``
    key is silently ignored, which is how a batch lands as a no-op."""
    recorder = Recorder()
    with client(recorder) as api:
        api.files.push_snapshot(
            DRIVE,
            ITEM,
            epoch=4,
            instance_id="laptop-7",
            changes=[{"path": "a.txt"}],
            if_match=ITEM_ROW,
        )
    assert _body(recorder.last) == {
        "epoch": 4,
        "instanceId": "laptop-7",
        "changes": [{"path": "a.txt"}],
    }


@pytest.mark.parametrize("verb", ["request_release", "force_release"])
def test_the_two_take_backs_carry_the_if_match_the_route_requires(verb: str) -> None:
    recorder = Recorder()
    with client(recorder) as api:
        getattr(api.files, verb)(DRIVE, ITEM, if_match=ITEM_ROW)
    assert recorder.last.headers["If-Match"] == "7"
    assert "Idempotency-Key" in recorder.last.headers


def test_my_leases_asks_for_mine_and_returns_the_rows() -> None:
    """The listing refuses the unfiltered question, so the flag is not optional;
    its success body is an array, not an object."""
    rows = [{"nodeId": ITEM, "epoch": 3, "machine": "MacBook Pro", "purpose": "mount"}]
    recorder = Recorder(
        {("GET", "/api/v1/files/drives/d1/leases"): [httpx.Response(200, json=rows)]}
    )
    with client(recorder) as api:
        assert api.files.my_leases(DRIVE) == rows
    assert recorder.last.url.params["mine"] == "true"


def test_a_leased_folder_names_its_holder_on_the_typed_error() -> None:
    """The 409's ``detail`` is the only place a caller learns who has the
    folder, so it has to survive the trip into the exception."""
    refusal = httpx.Response(
        409,
        json={
            "code": "files.leased",
            "message": "the folder is leased",
            "detail": {"holder": "ana@alkera.dev", "machine": "MacBook Pro"},
        },
    )
    recorder = Recorder({("POST", "/api/v1/files/drives/d1/items/n1/lease"): [refusal]})
    with client(recorder) as api, pytest.raises(AlkeraHTTPError) as raised:
        api.files.acquire_lease(DRIVE, ITEM, instance_id="i", machine_id="m", if_match=ITEM_ROW)
    assert raised.value.detail == {"holder": "ana@alkera.dev", "machine": "MacBook Pro"}


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        pytest.param(
            {"holder": "ana@alkera.dev", "machine": "MacBook Pro"},
            {"holder": "ana@alkera.dev", "machine": "MacBook Pro"},
            id="named",
        ),
        pytest.param(None, None, id="withheld"),
    ],
)
def test_the_envelope_is_read_before_the_flat_keys_beside_it(
    details: dict[str, str] | None, expected: dict[str, str] | None
) -> None:
    """A current server sends the envelope and, for older readers, the flat
    keys beside it; the envelope's code, message and details are the ones read."""
    error: dict[str, object] = {
        "type": "urn:alkera:error:files.leased",
        "code": "files.leased",
        "status": 409,
        "message": "the folder is leased",
        "trace_id": "t-1",
    }
    if details is not None:
        error["details"] = details
    body = {"error": error, "code": "stale.code", "message": "stale", "detail": {"stale": 1}}
    refusal = httpx.Response(409, json=body)
    recorder = Recorder({("POST", "/api/v1/files/drives/d1/items/n1/lease"): [refusal]})
    with client(recorder) as api, pytest.raises(AlkeraHTTPError) as raised:
        api.files.acquire_lease(DRIVE, ITEM, instance_id="i", machine_id="m", if_match=ITEM_ROW)
    assert (raised.value.code, raised.value.message) == ("files.leased", "the folder is leased")
    assert raised.value.detail == expected


def test_facts_an_older_server_put_straight_into_error_are_the_detail() -> None:
    """Before ``details``, the wrapped envelope carried a refusal's facts beside
    its code. A box keeps the CLI it was provisioned with and may meet either
    server, so the holder still comes through; the envelope's own members are
    not facts and are left out."""
    refusal = httpx.Response(
        409,
        json={
            "error": {
                "code": "files.leased",
                "message": "this folder is in use",
                "trace_id": "t-2",
                "holder": "ana@alkera.dev",
                "machine": "MacBook Pro",
            }
        },
    )
    recorder = Recorder({("POST", "/api/v1/files/drives/d1/items/n1/lease"): [refusal]})
    with client(recorder) as api, pytest.raises(AlkeraHTTPError) as raised:
        api.files.acquire_lease(DRIVE, ITEM, instance_id="i", machine_id="m", if_match=ITEM_ROW)
    assert raised.value.detail == {"holder": "ana@alkera.dev", "machine": "MacBook Pro"}


def test_a_withheld_holder_leaves_the_detail_empty() -> None:
    """A caller who may not read the node is told nothing about who holds it."""
    refusal = httpx.Response(409, json={"code": "files.leased", "message": "the folder is leased"})
    recorder = Recorder({("POST", "/api/v1/files/drives/d1/items/n1/lease"): [refusal]})
    with client(recorder) as api, pytest.raises(AlkeraHTTPError) as raised:
        api.files.acquire_lease(DRIVE, ITEM, instance_id="i", machine_id="m", if_match=ITEM_ROW)
    assert raised.value.detail is None


def test_a_rate_limited_call_waits_out_its_retry_after_and_is_replayed() -> None:
    """A box syncing a burst is told 429 with the seconds to wait. The limiter
    refuses before any handler runs, so the replay is the same request; it used
    to surface as the file not landing, with nothing waited out."""
    recorder = Recorder(
        {
            ("POST", "/api/v1/files/drives/d1/trash/op9/restore"): [
                httpx.Response(
                    429, headers={"Retry-After": "3"}, json={"error": {"code": "rate_limited"}}
                ),
                httpx.Response(200, json={"ok": True}),
            ]
        }
    )
    waits: list[float] = []

    _retrying_files(recorder, waits).restore(DRIVE, "op9")

    assert waits == [3.0]
    assert len(recorder.requests) == 2
    assert len({r.headers["Idempotency-Key"] for r in recorder.requests}) == 1
