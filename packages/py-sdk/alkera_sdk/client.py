"""Hand-written ergonomic wrapper over the generated Alkera API client.

The generated package at `alkera_sdk._generated` exposes one function per
endpoint, organised by operation_id (`live_health_live_get.sync`,
`login_api_v1_auth_login_post.sync_detailed`, …). That layout is faithful
but cumbersome — `AlkeraClient` groups operations into namespaces
(`api.health.live()`, `api.auth.login(email=…, password=…)`) and threads
a single httpx Client through them so cookie-based session auth is
preserved across calls without callers having to manage it.
"""

from __future__ import annotations

import itertools
import json
import os
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Final
from urllib.parse import quote

import httpx

from alkera_sdk._generated.api.auth import (
    login_api_v1_auth_login_post,
    logout_api_v1_auth_logout_post,
    me_api_v1_auth_me_get,
    signup_api_v1_auth_signup_post,
)
from alkera_sdk._generated.api.errors import (
    list_crash_reports_api_v1_errors_reports_get,
    report_client_error_api_v1_errors_events_post,
    submit_crash_report_api_v1_errors_reports_post,
)
from alkera_sdk._generated.api.health import (
    live_health_live_get,
    ready_health_ready_get,
)
from alkera_sdk._generated.client import Client
from alkera_sdk._generated.models.client_error_event import ClientErrorEvent
from alkera_sdk._generated.models.crash_report_create import CrashReportCreate
from alkera_sdk._generated.models.crash_report_read import CrashReportRead
from alkera_sdk._generated.models.crash_report_summary import CrashReportSummary
from alkera_sdk._generated.models.error_envelope import ErrorEnvelope
from alkera_sdk._generated.models.error_event_ack import ErrorEventAck
from alkera_sdk._generated.models.live_status import LiveStatus
from alkera_sdk._generated.models.login_request import LoginRequest
from alkera_sdk._generated.models.login_response import LoginResponse
from alkera_sdk._generated.models.me_read import MeRead
from alkera_sdk._generated.models.message_response import MessageResponse
from alkera_sdk._generated.models.ready_status import ReadyStatus
from alkera_sdk._generated.models.signup_request import SignupRequest
from alkera_sdk._generated.types import Response


class _HealthApi:
    """Liveness + readiness probes."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def live(self) -> Response[LiveStatus]:
        return live_health_live_get.sync_detailed(client=self._client)

    def ready(self, *, strict: bool = False) -> Response[ErrorEnvelope | ReadyStatus]:
        """``strict`` asks for a 503 the moment a dependency is unreachable,
        with no grace window; the default is the load balancer's answer."""
        return ready_health_ready_get.sync_detailed(client=self._client, strict=strict)


class _AuthApi:
    """Authentication endpoints. Cookies set by the server propagate
    automatically because the underlying httpx.Client owns a cookie jar.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    def login(self, *, email: str, password: str) -> LoginResponse:
        body = LoginRequest(email=email, password=password)
        result = login_api_v1_auth_login_post.sync_detailed(client=self._client, body=body)
        if result.parsed is None:
            raise _api_error("login", result)
        # ensure the parsed body is a LoginResponse, not a validation-error model.
        if not isinstance(result.parsed, LoginResponse):
            raise _api_error("login", result)
        return result.parsed

    def signup(self, body: SignupRequest) -> LoginResponse:
        result = signup_api_v1_auth_signup_post.sync_detailed(client=self._client, body=body)
        if result.parsed is None:
            raise _api_error("signup", result)
        if not isinstance(result.parsed, LoginResponse):
            raise _api_error("signup", result)
        return result.parsed

    def me(self) -> MeRead:
        result = me_api_v1_auth_me_get.sync_detailed(client=self._client)
        if result.parsed is None:
            raise _api_error("auth/me", result)
        return result.parsed

    def logout(self) -> MessageResponse:
        result = logout_api_v1_auth_logout_post.sync_detailed(client=self._client)
        if result.parsed is None:
            raise _api_error("logout", result)
        return result.parsed


class _ErrorsApi:
    """Crash reports + client-error events (POST /api/v1/errors/...).

    Used by the CLI / daemon to submit opt-in crash reports on the user's
    behalf (the daemon owns the authed token, so the VS Code extension routes
    its reports through the daemon to this namespace).
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    def submit_crash_report(self, body: CrashReportCreate) -> CrashReportRead:
        result = submit_crash_report_api_v1_errors_reports_post.sync_detailed(
            client=self._client, body=body
        )
        if not isinstance(result.parsed, CrashReportRead):
            raise _api_error("errors/reports", result)
        return result.parsed

    def list_crash_reports(self) -> list[CrashReportSummary]:
        result = list_crash_reports_api_v1_errors_reports_get.sync_detailed(client=self._client)
        if not isinstance(result.parsed, list):
            raise _api_error("errors/reports", result)
        return result.parsed

    def report_client_error(self, body: ClientErrorEvent) -> ErrorEventAck:
        result = report_client_error_api_v1_errors_events_post.sync_detailed(
            client=self._client, body=body
        )
        if not isinstance(result.parsed, ErrorEventAck):
            raise _api_error("errors/events", result)
        return result.parsed


#: Root of the Files REST surface.
_FILES: Final = "/api/v1/files"

#: An operation stops moving once it reaches one of these.
_TERMINAL_OPERATION_STATES: Final = frozenset({"done", "failed", "cancelled", "conflict"})
#: The longest wait between two polls of one operation.
OPERATION_POLL_MAX_INTERVAL_SECONDS: Final = 2.0


def _blake3_hex(data: bytes) -> str:
    """The part checksum the upload routes agree on.

    ``blake3`` is imported here rather than at module scope so the SDK keeps
    exactly the dependencies it declares: a caller who never uploads never
    needs it, and one who does can pass their own ``checksum`` callable.
    """
    try:
        from blake3 import blake3
    except ImportError as exc:  # pragma: no cover - exercised by the explicit-callable path
        raise RuntimeError(
            "alkera api: upload_file needs the 'blake3' package, "
            "or a checksum= callable of your own"
        ) from exc
    return blake3(data).hexdigest()


#: The longest pause a server may ask a call to take before its replay. A
#: caller blocks a thread for this long at most however large a number the
#: server names, and past it the answer is the failure rather than a client
#: that has stopped responding.
MAX_RETRY_AFTER_SECONDS: Final = 30.0


def _retry_after_seconds(response: httpx.Response) -> float:
    """The pause ``Retry-After`` asks for, in seconds, clamped to the cap.

    Only the delta-seconds form is read — that is what this API emits, and the
    HTTP-date form would be measured against a clock that is not the server's.
    Anything else (absent, negative, unparseable) means "no pause named", which
    is the behaviour a server that says nothing gets.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return 0.0
    try:
        named = float(raw.strip())
    except ValueError:
        return 0.0
    return min(max(named, 0.0), MAX_RETRY_AFTER_SECONDS)


class _FilesApi:
    """The Files REST surface (``/api/v1/files/…``).

    Unlike the other namespaces this one drives the underlying ``httpx.Client``
    directly rather than the generated per-endpoint functions. Files is the one
    surface whose contract lives in the *headers* and in the *transfer*, not
    only in the body: every mutation carries a minted ``Idempotency-Key`` and
    an ``If-Match``, a part carries ``X-Part-Checksum``, a download is a 302 to
    a single-use signed URL that has to be streamed rather than buffered, and
    listings page on an opaque marker. The generated wrappers model none of
    that; the OpenAPI document still governs the bodies.
    """

    #: Statuses worth one more attempt with the *same* idempotency key.
    #: 429 included: the rate limiter refuses before the handler runs, so a
    #: replay after its ``Retry-After`` is the same request, not a second one.
    _RETRY_STATUS: Final = frozenset({429, 502, 503, 504})

    def __init__(
        self,
        client: Client | httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._sleep = sleep

    # ---- the wire --------------------------------------------------------

    def _http(self) -> httpx.Client:
        client = self._client
        return client if isinstance(client, httpx.Client) else client.get_httpx_client()

    def on_client(self, http: httpx.Client) -> _FilesApi:
        """The same namespace speaking on ``http`` instead.

        A caller that holds a client of its own — the mount chain's fenced one,
        whose headers say which lease every write is under — needs the
        namespace bound to *that* client. A namespace left on the shared one
        would open its upload sessions without those headers, and the server
        would refuse them as somebody else's write.
        """
        return _FilesApi(http, sleep=self._sleep)

    def _send(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        content: bytes | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        if_match: str | Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        retries: int = 2,
    ) -> httpx.Response:
        """One Files request, carrying the headers the API requires.

        The ``Idempotency-Key`` is minted **once per call** and reused on every
        retry of that call — that is the whole point of the header: a retried
        ``POST …/complete`` must replay the first answer rather than start a
        second commit. Minting inside the retry loop would defeat it.
        """
        sent: dict[str, str] = dict(headers or {})
        if method != "GET":
            sent["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        if if_match is not None:
            sent["If-Match"] = _etag_of(if_match)

        attempt = 0
        while True:
            try:
                response = self._http().request(
                    method,
                    url,
                    json=json_body,
                    content=content,
                    params=dict(params) if params else None,
                    headers=sent,
                )
            except httpx.TransportError:
                if attempt >= retries:
                    raise
                attempt += 1
                continue
            if response.status_code in self._RETRY_STATUS and attempt < retries:
                attempt += 1
                # A refusal that names how long the server needs is a refusal
                # to come back inside that window: the API answers a request
                # parked behind a held row or a drained pool with a retryable
                # 503 and a Retry-After, and replaying it at once is one more
                # request it is in no state to answer.
                pause = _retry_after_seconds(response)
                if pause > 0:
                    self._sleep(pause)
                continue
            return response

    def _json(self, label: str, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise _http_error(label, response)
        parsed: Any = response.json()
        if not isinstance(parsed, dict):
            raise _http_error(label, response)
        return parsed

    def _none(self, label: str, response: httpx.Response) -> None:
        """A route whose success answer has no body worth reading.

        The refusal still has to be raised: swallowing it would turn a fenced
        release into a silent success and leave the caller believing it handed
        the folder back.
        """
        if response.status_code >= 400:
            raise _http_error(label, response)

    def _json_list(self, label: str, response: httpx.Response) -> list[dict[str, Any]]:
        """A route whose success body is a JSON *array*, not an object.

        ``POST …/tree`` is the one that matters: it answers 201 with the list of
        folders it created (empty when every path already existed), so putting
        it through :meth:`_json` turned every success into a raised error.
        """
        if response.status_code >= 400:
            raise _http_error(label, response)
        parsed: Any = response.json()
        if not isinstance(parsed, list):
            raise _http_error(label, response)
        return [row for row in parsed if isinstance(row, dict)]

    def _paged(
        self,
        label: str,
        url: str,
        *,
        params: Mapping[str, Any],
        limit: int,
    ) -> Iterator[dict[str, Any]]:
        """Follow an opaque ``nextMarker`` across pages, yielding items.

        Every listing, feed and search on the Files surface pages the same way,
        so they share one loop: a page without a marker ends it, and a marker
        the server already handed back is an error rather than a hang.
        """
        marker: str | None = None
        seen: set[str] = set()
        while True:
            query: dict[str, Any] = dict(params)
            query["limit"] = limit
            if marker is not None:
                query["marker"] = marker
            page = self._json(label, self._send("GET", url, params=query))
            yield from page.get("value") or []
            marker = page.get("nextMarker")
            if marker is None:
                return
            if marker in seen:
                raise RuntimeError(f"alkera api: {label} repeated a paging marker")
            seen.add(marker)

    # ---- items -----------------------------------------------------------

    def drive(self, *, chat_id: str | None = None) -> dict[str, Any]:
        """The caller's drive — or, for a box on its machine credential, which
        has no drive of its own, the drive of the chat it names."""
        return self._json(
            "files/drives",
            self._send("GET", f"{_FILES}/drives", params={"chatId": chat_id} if chat_id else None),
        )

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        return self._json(
            "files/item",
            self._send(
                "GET",
                f"{_FILES}/drives/{drive_id}/items/{item_id}",
                params={"select": select} if select else None,
            ),
        )

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        """The item at a drive path. Prefer :meth:`item`: a path is a name, and
        the node filed under it changes when somebody renames or moves it.

        Every segment is percent-encoded before it becomes a URL. A folder
        named ``What model are you?`` is otherwise a path that ends at
        ``are you`` with the rest read as a query string, so the lookup
        answers about the wrong node — or 404s — without anything looking
        wrong in the request.
        """
        quoted = quote(item_path.lstrip("/"), safe="/")
        return self._json(
            "files/item-by-path",
            self._send("GET", f"{_FILES}/drives/{drive_id}/root:/{quoted}"),
        )

    def item_under(self, drive_id: str, item_id: str, item_path: str) -> dict[str, Any]:
        """The item at a path BELOW ``item_id`` — the step down from a folder
        the caller holds. For a caller that reads nothing above that folder
        (a box on a chat's lease) the server spells the folder's path as its
        bare name, so this is the only path form it can address anything by.
        Segments are percent-encoded as :meth:`item_by_path` encodes them."""
        quoted = quote(item_path.strip("/"), safe="/")
        return self._json(
            "files/item-under",
            self._send("GET", f"{_FILES}/drives/{drive_id}/items/{item_id}:/{quoted}"),
        )

    def children(
        self,
        drive_id: str,
        item_id: str,
        *,
        limit: int = 100,
        order_by: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Every child of a folder, following the opaque ``nextMarker``.

        Yields items rather than pages, and stops the moment a page comes back
        without a marker. A server that handed back a marker it already used
        would loop forever, so a repeat is an error rather than a hang.
        """
        params: dict[str, Any] = dict(filters or {})
        if order_by is not None:
            params["orderBy"] = order_by
        return self._paged(
            "files/children",
            f"{_FILES}/drives/{drive_id}/items/{item_id}/children",
            params=params,
            limit=limit,
        )

    def search(
        self,
        drive_id: str,
        item_id: str,
        *,
        filters: Mapping[str, Any],
        limit: int = 100,
        order_by: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """One folder's direct children, narrowed by the listing filter chips.

        This is a *filtered listing*, not a name search — see
        :meth:`search_names` for the drive-wide ``/search`` route. The route
        refuses a chip it does not know rather than silently returning an
        unfiltered page, so a misspelled one surfaces as an error here too
        instead of as wrong results.
        """
        return self.children(drive_id, item_id, limit=limit, order_by=order_by, filters=filters)

    def search_names(
        self,
        drive_id: str,
        *,
        q: str = "",
        scope_item_id: str | None = None,
        filters: Mapping[str, Any] | None = None,
        limit: int = 100,
        order_by: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Name search across a drive, or across one subtree of it.

        The drive-wide ``/search`` route: a substring match on names over a
        whole subtree, which is what "search" has to mean for a client typing
        into a box — unlike :meth:`search`, which only ever sees one folder's
        direct children. ``scope_item_id`` narrows it by becoming
        ``scope=folder:{id}``; the same listing filter chips ride alongside.
        """
        params: dict[str, Any] = dict(filters or {})
        params["q"] = q
        params["scope"] = f"folder:{scope_item_id}" if scope_item_id is not None else "drive"
        if order_by is not None:
            params["orderBy"] = order_by
        return self._paged(
            "files/search", f"{_FILES}/drives/{drive_id}/search", params=params, limit=limit
        )

    def recent(
        self,
        drive_id: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """What this caller touched lately, newest first.

        The feed is ordered by the server (mtime, descending), so unlike the
        other listings it takes no ``orderBy``.
        """
        return self._paged(
            "files/recent",
            f"{_FILES}/drives/{drive_id}/recent",
            params=dict(filters or {}),
            limit=limit,
        )

    def starred(
        self,
        drive_id: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 100,
        order_by: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """The caller's own starred nodes."""
        params: dict[str, Any] = dict(filters or {})
        if order_by is not None:
            params["orderBy"] = order_by
        return self._paged(
            "files/starred", f"{_FILES}/drives/{drive_id}/starred", params=params, limit=limit
        )

    def shared_with_me(
        self,
        drive_id: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 100,
        order_by: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """The roots of the grants made to this caller, their teams and org."""
        params: dict[str, Any] = dict(filters or {})
        if order_by is not None:
            params["orderBy"] = order_by
        return self._paged(
            "files/shared-with-me",
            f"{_FILES}/drives/{drive_id}/sharedWithMe",
            params=params,
            limit=limit,
        )

    def activity(
        self,
        drive_id: str,
        item_id: str,
        *,
        ancestor: bool = False,
        limit: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """One node's history — or its whole subtree's, with ``ancestor=True``.

        ``ancestor`` is sent only when it is on: the route reads the raw query
        string and treats the literal ``"true"`` as the switch, so spelling a
        Python ``False`` onto the wire would be a value it never asked for.
        """
        params: dict[str, Any] = {"ancestor": "true"} if ancestor else {}
        return self._paged(
            "files/activity",
            f"{_FILES}/drives/{drive_id}/items/{item_id}/activity",
            params=params,
            limit=limit,
        )

    def create_child(
        self,
        drive_id: str,
        parent_id: str,
        name: str,
        *,
        kind: str = "folder",
        subtype: str | None = None,
        symlink_target: str | None = None,
        conflict_behavior: str = "fail",
    ) -> dict[str, Any]:
        """Add one folder, symlink or special node under a folder.

        Files are absent by design: bytes arrive through :meth:`put_content` or
        an upload session, so a ``kind="file"`` here would create a node with no
        version that every listing would render as an empty file nobody wrote.
        ``conflictBehavior`` is ``fail`` or ``rename`` only — ``replace`` is a
        *content* mode and a folder has no content to replace.
        """
        body: dict[str, Any] = {
            "kind": kind,
            "name": name,
            "conflictBehavior": conflict_behavior,
        }
        if subtype is not None:
            body["subtype"] = subtype
        if symlink_target is not None:
            body["symlinkTarget"] = symlink_target
        return self._json(
            "files/create-child",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/items/{parent_id}/children",
                json_body=body,
            ),
        )

    def create_folder(
        self,
        drive_id: str,
        parent_id: str,
        name: str,
        *,
        conflict_behavior: str = "fail",
    ) -> dict[str, Any]:
        return self.create_child(
            drive_id, parent_id, name, kind="folder", conflict_behavior=conflict_behavior
        )

    def create_tree(
        self, drive_id: str, item_id: str, paths: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Create a dropped directory's folder skeleton in one call.

        The route answers 201 with the *list* of folders it actually created —
        empty when every path was already there, because the call walks into an
        existing folder instead of re-creating it. That list is the answer, so
        it is returned as one rather than forced into an object.
        """
        return self._json_list(
            "files/create-tree",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/tree",
                json_body={"paths": list(paths)},
            ),
        )

    def patch_item(
        self,
        drive_id: str,
        item_id: str,
        *,
        if_match: str | Mapping[str, Any],
        name: str | None = None,
        parent_id: str | None = None,
        attrs: Mapping[str, Any] | None = None,
        conflict_behavior: str | None = None,
        label: str = "files/patch",
    ) -> dict[str, Any]:
        """Rename, move and set attributes — any combination, in one call.

        A move too large to run inline answers ``202`` with the *operation*
        rather than the node, so the answer is handed back as it came: a caller
        that sees an ``id`` and a ``state`` polls :meth:`await_operation`, and
        one that sees an item is already done. Turning the 202 into an error
        here would make the large-move path unreachable from the SDK.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if parent_id is not None:
            body["parentId"] = parent_id
        if attrs is not None:
            body["attrs"] = dict(attrs)
        if not body:
            raise ValueError("alkera api: files/patch was given nothing to change")
        return self._json(
            label,
            self._send(
                "PATCH",
                f"{_FILES}/drives/{drive_id}/items/{item_id}",
                json_body=body,
                params=(
                    {"conflict_behavior": conflict_behavior}
                    if conflict_behavior is not None
                    else None
                ),
                if_match=if_match,
            ),
        )

    def rename(
        self,
        drive_id: str,
        item_id: str,
        name: str,
        *,
        if_match: str | Mapping[str, Any],
        conflict_behavior: str = "fail",
    ) -> dict[str, Any]:
        return self.patch_item(
            drive_id,
            item_id,
            name=name,
            if_match=if_match,
            conflict_behavior=conflict_behavior,
            label="files/rename",
        )

    def move(
        self,
        drive_id: str,
        item_id: str,
        parent_id: str,
        *,
        if_match: str | Mapping[str, Any],
        name: str | None = None,
    ) -> dict[str, Any]:
        return self.patch_item(
            drive_id,
            item_id,
            parent_id=parent_id,
            name=name,
            if_match=if_match,
            label="files/move",
        )

    def put_content(
        self,
        drive_id: str,
        item_id: str,
        data: bytes,
        *,
        if_match: str | Mapping[str, Any],
        conflict_behavior: str = "replace",
        mime: str | None = None,
    ) -> dict[str, Any]:
        """Write a whole small file onto an existing node in one request.

        ``200`` means the node's head already held exactly these bytes and no
        version was written; ``201`` means a new version is the head. Both are
        the item, so both come back — the caller tells them apart by the version
        the item carries, never by the SDK raising on one of them.
        """
        headers = {"Content-Type": mime} if mime else None
        return self._json(
            "files/put-content",
            self._send(
                "PUT",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/content",
                content=data,
                params={"conflictBehavior": conflict_behavior},
                headers=headers,
                if_match=if_match,
            ),
        )

    def bulk(self, drive_id: str, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Apply a batch of tree changes in one request.

        A small batch answers ``200`` with one row per item — each carrying the
        status and body that change would have had as its own request — and a
        batch too big to run inline answers ``202`` with the operation instead.
        Both are objects and the body says which arrived, so a caller branches
        on ``responses`` versus ``state`` rather than on a status the SDK would
        otherwise have to leak separately.
        """
        return self._json(
            "files/bulk",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/bulk",
                json_body={"items": [dict(item) for item in items]},
            ),
        )

    def copy(
        self,
        drive_id: str,
        item_id: str,
        parent_id: str,
        *,
        name: str | None = None,
        conflict_behavior: str = "rename",
    ) -> dict[str, Any]:
        """Copy a node into another folder. Always ``202`` + an operation."""
        body: dict[str, Any] = {
            "parentId": parent_id,
            "conflictBehavior": conflict_behavior,
        }
        if name is not None:
            body["name"] = name
        return self._json(
            "files/copy",
            self._send("POST", f"{_FILES}/drives/{drive_id}/items/{item_id}/copy", json_body=body),
        )

    def download_subtree(
        self, drive_id: str, item_id: str, *, if_match: str | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Start a ZIP64 archive of a subtree. ``202`` + an operation.

        The walk runs inside the request, so the operation comes back already
        listing — by id — everything the archive will not contain; the bytes
        appear only when its ``resultUrl`` is redeemed.
        """
        return self._json(
            "files/download-subtree",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/download",
                if_match=if_match,
            ),
        )

    def trash(
        self, drive_id: str, item_id: str, *, if_match: str | Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._json(
            "files/trash",
            self._send("DELETE", f"{_FILES}/drives/{drive_id}/items/{item_id}", if_match=if_match),
        )

    def list_trash(self, drive_id: str) -> dict[str, Any]:
        return self._json("files/trash", self._send("GET", f"{_FILES}/drives/{drive_id}/trash"))

    def restore(self, drive_id: str, op_id: str) -> dict[str, Any]:
        """Undo one trash operation — the whole set it removed, by its id."""
        return self._json(
            "files/restore",
            self._send("POST", f"{_FILES}/drives/{drive_id}/trash/{op_id}/restore"),
        )

    def empty_trash(self, drive_id: str) -> dict[str, Any]:
        """Purge every trashed root this caller may delete."""
        return self._json(
            "files/trash-empty",
            self._send("POST", f"{_FILES}/drives/{drive_id}/trash/empty"),
        )

    # ---- versions --------------------------------------------------------

    def versions(self, drive_id: str, item_id: str) -> dict[str, Any]:
        """Every version of one file, oldest first, with the head marked."""
        return self._json(
            "files/versions",
            self._send("GET", f"{_FILES}/drives/{drive_id}/items/{item_id}/versions"),
        )

    def restore_version(
        self,
        drive_id: str,
        item_id: str,
        version_id: str,
        *,
        if_match: str | Mapping[str, Any],
    ) -> dict[str, Any]:
        """Make an older version the head again by appending it as a new one."""
        return self._json(
            "files/version-restore",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/versions/{version_id}/restore",
                if_match=if_match,
            ),
        )

    def download_version(
        self,
        drive_id: str,
        item_id: str,
        version_id: str,
        dest: str | os.PathLike[str],
    ) -> Path:
        """Stream one older version's bytes to ``dest``, through its 302."""
        return self._follow_and_stream(
            "files/download-version",
            f"{_FILES}/drives/{drive_id}/items/{item_id}/versions/{version_id}/content",
            dest,
        )

    # ---- conflicts -------------------------------------------------------

    def conflicts(self, drive_id: str) -> dict[str, Any]:
        """The unresolved divergences a returning lease holder left behind."""
        return self._json(
            "files/conflicts", self._send("GET", f"{_FILES}/drives/{drive_id}/conflicts")
        )

    def resolve_conflict(self, drive_id: str, conflict_id: str, *, keep: str) -> dict[str, Any]:
        """Resolve one conflict by keeping ``mine``, ``theirs`` or ``both``."""
        return self._json(
            "files/conflict-resolve",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/conflicts/{conflict_id}/resolve",
                json_body={"keep": keep},
            ),
        )

    def star(
        self, drive_id: str, item_id: str, *, if_match: str | Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._json(
            "files/star",
            self._send(
                "PUT", f"{_FILES}/drives/{drive_id}/items/{item_id}/star", if_match=if_match
            ),
        )

    def unstar(
        self, drive_id: str, item_id: str, *, if_match: str | Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._json(
            "files/unstar",
            self._send(
                "DELETE", f"{_FILES}/drives/{drive_id}/items/{item_id}/star", if_match=if_match
            ),
        )

    # ---- sharing ---------------------------------------------------------

    def permissions(
        self, drive_id: str, item_id: str, *, effective: bool = False
    ) -> dict[str, Any]:
        """The direct grants on a node, or — with ``effective`` — the whole set.

        The effective set carries each entry's ``origin`` and the ancestor that
        granted it, which is the only way a caller can explain why someone has
        access to a node nobody granted directly.
        """
        return self._json(
            "files/permissions",
            self._send(
                "GET",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/permissions",
                params={"effective": "true"} if effective else None,
            ),
        )

    def grant_permission(
        self,
        drive_id: str,
        item_id: str,
        *,
        principal: Mapping[str, Any],
        role: str,
        if_match: str | Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "files/grant",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/permissions",
                json_body={"principal": dict(principal), "role": role},
                if_match=if_match,
            ),
        )

    def revoke_permission(
        self,
        drive_id: str,
        item_id: str,
        share_id: str,
        *,
        if_match: str | Mapping[str, Any],
    ) -> httpx.Response:
        response = self._send(
            "DELETE",
            f"{_FILES}/drives/{drive_id}/items/{item_id}/permissions/{share_id}",
            if_match=if_match,
        )
        if response.status_code >= 400:
            raise _http_error("files/revoke", response)
        return response

    # ---- leases ----------------------------------------------------------

    def acquire_lease(
        self,
        drive_id: str,
        item_id: str,
        *,
        instance_id: str,
        machine_id: str,
        if_match: str | Mapping[str, Any],
        purpose: str = "mount",
        ttl: int | None = None,
        inbound: bool = False,
        live: bool = False,
        retake: bool = False,
    ) -> dict[str, Any]:
        """Take the folder for one instance on one machine.

        Both ids are required because they answer different questions: the
        instance is the *holder* the server fences on, and the machine is what
        a human is told when someone else has the folder. A refused 409 carries
        the holder in :attr:`AlkeraHTTPError.detail` when the caller may read
        the node it names, and carries nothing when they may not.

        ``inbound`` asks for a lease that admits other people's writes into the
        subtree, and ``live`` for one served with a streaming cadence. Both are
        sent only when asked for, so a caller that wants neither puts the same
        bytes on the wire it always did.

        ``retake`` says the holder is asking again for a lease it believes is
        still its own, after a fenced beat: the server re-grants a lapse and
        refuses a lease it ended (``files.lease_ended``).
        """
        body: dict[str, Any] = {
            "instanceId": instance_id,
            "machineId": machine_id,
            "purpose": purpose,
        }
        if ttl is not None:
            body["ttl"] = ttl
        if inbound:
            body["inbound"] = True
        if live:
            body["live"] = True
        if retake:
            body["retake"] = True
        return self._json(
            "files/lease",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/lease",
                json_body=body,
                if_match=if_match,
            ),
        )

    def heartbeat_lease(
        self, drive_id: str, item_id: str, *, epoch: int, instance_id: str
    ) -> dict[str, Any]:
        """Keep the lease alive. 409 ``files.lease_fenced`` means it is gone."""
        return self._json(
            "files/lease-heartbeat",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/lease/heartbeat",
                json_body={"epoch": epoch, "instanceId": instance_id},
                headers=_fence(epoch, instance_id),
            ),
        )

    def heartbeat_leases(self, drive_id: str, leases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep every named lease in one call: ``[{nodeId, epoch, instanceId}]``
        in, one ``{nodeId, verdict, grant}`` per lease out, in the order asked.
        ``verdict`` is ``renewed`` (``grant`` is the per-lease beat's answer),
        ``superseded`` (its 409) or ``gone`` (its 404). A server older than the
        route answers 404 for the whole call."""
        answer = self._json(
            "files/leases-heartbeat",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/leases/heartbeat",
                json_body={"leases": leases},
            ),
        )
        verdicts = answer.get("leases")
        return [v for v in verdicts if isinstance(v, dict)] if isinstance(verdicts, list) else []

    def release_lease(
        self,
        drive_id: str,
        item_id: str,
        *,
        epoch: int,
        instance_id: str,
        if_match: str | Mapping[str, Any],
        final: Sequence[Mapping[str, Any]] | None = None,
        unsynced_count: int | None = None,
        unsynced_paths: Sequence[str] | None = None,
        ending: str | None = None,
    ) -> None:
        """Hand the folder back, with the final batch in the same transaction.

        ``ending`` says the hand-back ends the chat the folder belongs to (a
        box putting it to sleep, ``idle`` or ``evicted``): the server records
        the chat asleep in the same transaction.

        ``final=None`` means "no final batch"; an empty sequence is a batch that
        happens to carry nothing, which is what an unmount with no local edits
        sends. The route answers with no body, so there is nothing to return.

        ``unsynced_count`` is how many files the holder could not land before
        it let go, and ``unsynced_paths`` the first of them by path; both are
        left off the body when the holder has nothing to say.
        """
        body: dict[str, Any] = {"epoch": epoch, "instanceId": instance_id}
        if final is not None:
            body["final"] = [dict(entry) for entry in final]
        if unsynced_count is not None:
            body["unsyncedCount"] = unsynced_count
            body["unsyncedPaths"] = list(unsynced_paths or ())
        if ending is not None:
            body["ending"] = ending
        self._none(
            "files/lease-release",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/lease/release",
                json_body=body,
                headers=_fence(epoch, instance_id),
                if_match=if_match,
            ),
        )

    def push_snapshot(
        self,
        drive_id: str,
        item_id: str,
        *,
        epoch: int,
        instance_id: str,
        changes: Sequence[Mapping[str, Any]],
        if_match: str | Mapping[str, Any],
    ) -> None:
        """One fenced snapshot from a mount: the batch a watcher accumulated."""
        self._none(
            "files/snapshot",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/snapshots",
                json_body={
                    "epoch": epoch,
                    "instanceId": instance_id,
                    "changes": [dict(entry) for entry in changes],
                },
                headers=_fence(epoch, instance_id),
                if_match=if_match,
            ),
        )

    def request_release(
        self, drive_id: str, item_id: str, *, if_match: str | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Ask the holder for the folder back; the answer names them."""
        return self._json(
            "files/lease-request-release",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/lease/request-release",
                if_match=if_match,
            ),
        )

    def force_release(
        self, drive_id: str, item_id: str, *, if_match: str | Mapping[str, Any]
    ) -> dict[str, Any]:
        """A manager takes it back, leaving the holder one TTL of grace."""
        return self._json(
            "files/lease-force-release",
            self._send(
                "POST",
                f"{_FILES}/drives/{drive_id}/items/{item_id}/lease/force-release",
                if_match=if_match,
            ),
        )

    def my_leases(self, drive_id: str) -> list[dict[str, Any]]:
        """My mounts across the drive.

        ``mine=true`` is sent always: the route refuses the unfiltered question
        (it would have to be filtered by readability per row), so a client that
        omitted the flag only ever got a 400.
        """
        return self._json_list(
            "files/leases",
            self._send("GET", f"{_FILES}/drives/{drive_id}/leases", params={"mine": "true"}),
        )

    # ---- delta + operations ----------------------------------------------

    def delta(self, drive_id: str, *, token: str | None = None) -> Iterator[dict[str, Any]]:
        """Every delta page from ``token`` forward.

        Stops on the first page with no ``nextLink`` — that page carries the
        ``deltaLink`` the caller stores for next time, so it is yielded before
        the generator ends.
        """
        url = f"{_FILES}/drives/{drive_id}/delta"
        params: dict[str, Any] | None = {"token": token} if token else None
        while True:
            page = self._json("files/delta", self._send("GET", url, params=params))
            yield page
            next_link = page.get("nextLink")
            if not next_link:
                return
            url, params = str(next_link), None

    def operation(self, drive_id: str, operation_id: str) -> dict[str, Any]:
        return self._json(
            "files/operation",
            self._send("GET", f"{_FILES}/drives/{drive_id}/operations/{operation_id}"),
        )

    def cancel_operation(self, drive_id: str, operation_id: str) -> dict[str, Any]:
        return self._json(
            "files/operation-cancel",
            self._send("POST", f"{_FILES}/drives/{drive_id}/operations/{operation_id}/cancel"),
        )

    def undo_operation(self, drive_id: str, operation_id: str) -> dict[str, Any]:
        return self._json(
            "files/operation-undo",
            self._send("POST", f"{_FILES}/drives/{drive_id}/operations/{operation_id}/undo"),
        )

    def await_operation(
        self,
        drive_id: str,
        operation_id: str,
        *,
        timeout: float = 60.0,
        interval: float = 0.2,
        max_interval: float = OPERATION_POLL_MAX_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """Poll an operation until it reaches a terminal state, or raise
        ``TimeoutError`` at ``timeout``.

        The wait between polls starts at ``interval`` and doubles up to
        ``max_interval``: a commit that lands at once is seen at once, and one
        the server is slow on is not asked about ten times a second for as
        long as the caller waits — a box handing back a large folder spun
        eleven thousand polls in forty minutes on a fixed interval.

        ``sleep`` and ``now`` are parameters so a test can drive the loop
        without spending wall-clock time in it.
        """
        deadline = now() + timeout
        wait = interval
        while True:
            state = self.operation(drive_id, operation_id)
            if state.get("state") in _TERMINAL_OPERATION_STATES:
                return state
            left = deadline - now()
            if left <= 0:
                raise TimeoutError(
                    f"alkera api: operation {operation_id} still {state.get('state')!r}"
                )
            sleep(min(wait, left))
            wait = min(wait * 2, max(max_interval, interval))

    # ---- uploads ---------------------------------------------------------

    def open_upload(
        self,
        *,
        parent_id: str,
        name: str,
        declared_size: int,
        mime: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "parentId": parent_id,
            "name": name,
            "declaredSize": declared_size,
        }
        if mime is not None:
            body["mime"] = mime
        return self._json(
            "files/upload-open",
            self._send(
                "POST", f"{_FILES}/uploads", json_body=body, idempotency_key=idempotency_key
            ),
        )

    def put_part(
        self,
        session_id: str,
        part_no: int,
        data: bytes,
        *,
        checksum: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "files/upload-part",
            self._send(
                "PUT",
                f"{_FILES}/uploads/{session_id}/parts/{part_no}",
                content=data,
                headers={
                    "X-Part-Checksum": checksum,
                    "Content-Type": "application/octet-stream",
                },
                idempotency_key=idempotency_key,
            ),
        )

    def complete_upload(
        self,
        session_id: str,
        parts: Sequence[Mapping[str, Any]],
        *,
        conflict_behavior: str = "fail",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "files/upload-complete",
            self._send(
                "POST",
                f"{_FILES}/uploads/{session_id}/complete",
                json_body={
                    "parts": [dict(part) for part in parts],
                    "conflictBehavior": conflict_behavior,
                },
                idempotency_key=idempotency_key,
            ),
        )

    def upload_status(self, session_id: str) -> dict[str, Any]:
        return self._json(
            "files/upload-status", self._send("GET", f"{_FILES}/uploads/{session_id}")
        )

    def abort_upload(self, session_id: str) -> httpx.Response:
        response = self._send("DELETE", f"{_FILES}/uploads/{session_id}")
        if response.status_code >= 400:
            raise _http_error("files/upload-abort", response)
        return response

    def upload_file(
        self,
        source: str | os.PathLike[str],
        *,
        drive_id: str,
        parent_id: str,
        name: str | None = None,
        mime: str | None = None,
        conflict_behavior: str = "fail",
        checksum: Callable[[bytes], str] = _blake3_hex,
        wait: bool = True,
        **await_kwargs: Any,
    ) -> dict[str, Any]:
        """Open a session, stream ``source`` part by part, and commit it.

        The parts are cut to the ``partSize`` the *server* chose at open: the
        server owns the part geometry, and a client that picked its own would
        have the commit refuse its part table. Each part carries the checksum
        the server agrees with before it stores anything, which is what makes a
        retried part a no-op rather than a corruption.
        """
        path = Path(source)
        size = path.stat().st_size
        session = self.open_upload(
            parent_id=parent_id,
            name=name or path.name,
            declared_size=size,
            mime=mime,
        )
        session_id = str(session["uploadId"])
        part_size = int(session["partSize"])

        parts: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            for part_no in itertools.count(1):
                chunk = handle.read(part_size)
                if not chunk:
                    break
                digest = checksum(chunk)
                self.put_part(session_id, part_no, chunk, checksum=digest)
                parts.append({"partNo": part_no, "size": len(chunk), "checksum": digest})

        operation = self.complete_upload(session_id, parts, conflict_behavior=conflict_behavior)
        if not wait:
            return operation
        return self.await_operation(drive_id, str(operation["id"]), **await_kwargs)

    # ---- download --------------------------------------------------------

    def download(self, drive_id: str, item_id: str, dest: str | os.PathLike[str]) -> Path:
        """Follow the 302 to the signed content URL and stream it to ``dest``."""
        return self._follow_and_stream(
            "files/download", f"{_FILES}/drives/{drive_id}/items/{item_id}/content", dest
        )

    def _follow_and_stream(self, label: str, url: str, dest: str | os.PathLike[str]) -> Path:
        """Redeem one content redirect and write its body to ``dest``.

        The redirect is followed by hand rather than with ``follow_redirects``
        so the single-use signed URL is fetched exactly once, and the body is
        streamed so a large file never sits in memory.
        """
        response = self._send("GET", url)
        if response.status_code not in (301, 302, 303, 307, 308):
            raise _http_error(label, response)
        location = response.headers.get("location")
        if not location:
            raise RuntimeError(f"alkera api: {label} redirect carried no Location")

        out = Path(dest)
        with self._http().stream("GET", location) as streamed:
            if streamed.status_code >= 400:
                streamed.read()
                raise _http_error(label, streamed)
            with out.open("wb") as handle:
                for chunk in streamed.iter_bytes():
                    handle.write(chunk)
        return out


def _etag_of(value: str | Mapping[str, Any]) -> str:
    """The ``If-Match`` for a mutation — an etag, or the item that carries one."""
    if isinstance(value, str):
        return value
    for field in ("etag", "eTag"):
        found = value.get(field)
        if found is not None:
            return str(found)
    raise ValueError("alkera api: this item carries no etag to send as If-Match")


def _fence(epoch: int, instance_id: str) -> dict[str, str]:
    """The fencing headers a lease-holder's write must carry."""
    return {"X-Alkera-Lease-Instance": instance_id, "X-Alkera-Lease-Epoch": str(epoch)}


class AlkeraHTTPError(RuntimeError):
    """A call the API refused, with the facts a caller acts on.

    Stays a :class:`RuntimeError` so callers written against the old wrapper
    keep catching it. What is new is that they no longer have to read the
    sentence: ``status`` separates the classes of failure, ``code`` is the
    API's own error code (``not_found``, ``conflict``, …) for the ones that
    carry it, and ``trace_id`` is the request id to quote in a bug report.
    """

    def __init__(
        self,
        *,
        label: str,
        status: int,
        code: str | None,
        message: str,
        trace_id: str | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        said = f"alkera api: {label} returned {status}"
        if code is not None:
            said += f" ({code})"
        if message:
            said += f" — {message}"
        super().__init__(said)
        self.label = label
        self.status = status
        self.code = code
        self.message = message
        self.trace_id = trace_id
        self.detail = dict(detail) if detail else None
        """The refusal's own facts, when it carried any — a leased folder's
        holder and machine, for instance. ``None`` when the API withheld them,
        which for the lease family means the caller may not read that node."""


class AlkeraAuthError(AlkeraHTTPError):
    """A 401 or 403 — the credential was rejected or is not enough for this.

    Its own type because it is the one refusal a caller answers by asking the
    person to sign in again rather than by showing them an error.
    """


#: The members every error envelope has, which are not a refusal's own facts.
_ENVELOPE_MEMBERS: Final = frozenset({"type", "code", "status", "message", "trace_id", "details"})


def _refusal(
    label: str,
    status: int,
    body: bytes,
    headers: Mapping[str, str],
) -> AlkeraHTTPError:
    """Build the typed error for one refused response."""
    text = body.decode("utf-8", errors="replace")
    code: str | None = None
    detail: Mapping[str, Any] | None = None
    message = text[:200]
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        # Every error is the envelope ``{"error": {code, message, details}}``;
        # a server older than it answered Files refusals flat, as ``{code,
        # message, detail}``, and still sends those keys beside the envelope.
        # The envelope is read when present, so a refusal's own facts (the
        # holder of a leased folder, say) come from one place.
        nested = parsed.get("error")
        layer: Mapping[str, Any] = nested if isinstance(nested, Mapping) else parsed
        raw_code = layer.get("code")
        code = str(raw_code) if isinstance(raw_code, str) else None
        raw_message = layer.get("message")
        # A body that names a code and nothing else says all it has to say in
        # the code; echoing its JSON back would only repeat it.
        message = str(raw_message) if isinstance(raw_message, str) else ("" if code else message)
        raw_detail = layer.get("details") if layer is nested else layer.get("detail")
        if isinstance(raw_detail, Mapping):
            detail = raw_detail
        elif layer is nested:
            # A server older than ``details`` put a refusal's facts straight
            # into ``error``. Only facts count: an envelope that carries
            # nothing beyond its own members carries no detail, which is how a
            # withheld holder reads as ``None``.
            facts = {k: v for k, v in layer.items() if k not in _ENVELOPE_MEMBERS}
            detail = facts or None
    kind = AlkeraAuthError if status in (401, 403) else AlkeraHTTPError
    return kind(
        label=label,
        status=status,
        code=code,
        message=message,
        trace_id=headers.get("x-request-id"),
        detail=detail,
    )


def _http_error(label: str, response: httpx.Response) -> AlkeraHTTPError:
    return _refusal(label, response.status_code, response.content, response.headers)


def _api_error(label: str, result: Response[Any]) -> AlkeraHTTPError:
    return _refusal(label, int(result.status_code), result.content, result.headers)


class AlkeraClient:
    """Synchronous client for the Alkera API.

    Holds a single underlying ``httpx.Client``; cookies set by login() (or
    by ``signup``) persist across subsequent calls, so the typical flow is:

        with AlkeraClient(base_url="http://localhost:8000") as api:
            api.auth.login(email="…", password="…")
            user = api.auth.me()
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8000",
        token: str | None = None,
        org_id: str | None = None,
        timeout: httpx.Timeout | float | None = 5.0,
        httpx_args: dict[str, Any] | None = None,
    ) -> None:
        """Construct a client.

        Args:
            base_url: API root, e.g. ``http://localhost:8000``.
            token: Optional CLI JWT (from ``alkera login``). When set, every
                request carries ``Authorization: Bearer <token>``.
            org_id: Optional org the client believes ``token`` is for. When
                set, every request carries ``X-Alkera-Org``, and the server
                answers 409 ``org_changed`` if it is not the token's own org.
            timeout: httpx timeout (seconds or ``httpx.Timeout``).
            httpx_args: Forwarded to the underlying ``httpx.Client`` —
                useful for injecting a transport in tests.
        """
        client_kwargs: dict[str, Any] = {"base_url": base_url}
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if org_id:
            headers["X-Alkera-Org"] = org_id
        if headers:
            client_kwargs["headers"] = headers
        if timeout is not None:
            client_kwargs["timeout"] = (
                timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
            )
        if httpx_args is not None:
            client_kwargs["httpx_args"] = httpx_args
        self._client = Client(**client_kwargs)
        self.health = _HealthApi(self._client)
        self.auth = _AuthApi(self._client)
        self.errors = _ErrorsApi(self._client)
        self.files = _FilesApi(self._client)

    @property
    def raw_client(self) -> Client:
        """Escape hatch: hand the raw generated client to a per-endpoint
        function from ``alkera_sdk._generated.api`` for routes the wrapper
        hasn't surfaced yet.
        """
        return self._client

    def __enter__(self) -> AlkeraClient:
        self._client.get_httpx_client().__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.get_httpx_client().__exit__(exc_type, exc, tb)


def files_namespace(http: httpx.Client) -> _FilesApi:
    """The Files namespace over a caller's own ``httpx.Client``.

    The CLI's mount chain already owns an authenticated client (the fencing
    headers are set on it for the length of a push), so it needs the namespace
    bound to *that* client rather than a second one — otherwise the mount's
    lease calls and its writes would travel on different connections with
    different headers.
    """
    return _FilesApi(http)


__all__ = ["AlkeraAuthError", "AlkeraClient", "AlkeraHTTPError", "files_namespace"]
