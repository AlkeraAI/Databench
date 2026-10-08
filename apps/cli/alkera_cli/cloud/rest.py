"""The daemon's REST client to the backend: the box's bearer + agent assertion.

Every call carries ``Authorization: Bearer <bearer>`` — an org box's device
JWT, or a platform box's machine credential — and, when the client names an
agent, the two agent headers built by :func:`alkera_core.authz.agent_headers`
(never spelled here), so the backend resolves the caller as an agent acting
for the user who signed the machine in and records that chain on every
decision. ``agent_id`` names the actor: the machine while the service is
discovering chats, the chat id once a mirror speaks for one (``for_agent``
clones the client with a new id). ``None`` asserts no agent at all: a box on
its machine credential has nothing to assert until the claim tells it which
machine it is — the backend admits an assertion on that credential only when
it names the box's own machine or a chat bound to it.

No credential ever rides a URL or a log line: the token lives in a header,
the socket ticket is minted here and handed to the transport, which offers it
in the subprotocol list. A non-2xx answer is a :class:`CloudApiError` carrying
the status and the body's ``{code, message}`` when the route named one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime
from typing import Any

import httpx
from alkera_core.auth.machine_token import machine_credential_headers
from alkera_core.authz import agent_headers
from alkera_core.schemas.compute import (
    MachineClaimRequest,
    MachineHeartbeatRequest,
    MachineRegisterRequest,
)
from alkera_core.schemas.compute_machines import MachineResources
from alkera_core.schemas.realtime import WS_PATH, WsTicketResponse
from httpx_sse import aconnect_sse

from alkera_cli.account.binding import FixedCredential
from alkera_cli.box_capabilities import DAEMON_CAPABILITIES
from alkera_cli.cloud.attachments import RestContentFetcher
from alkera_cli.cloud.box_auth import BoxCredential, bearer_headers
from alkera_cli.cloud.limits import heartbeat_timeout_seconds, rest_timeout_seconds
from alkera_cli.harness.sandbox import SandboxSettings

#: How long the event stream may go without a single byte before the box calls
#: it dead and reconnects. The server writes a keepalive comment every fifteen
#: seconds (``realtime_sse_keepalive_seconds``), so four missed in a row is a
#: stream nothing is feeding — a connection held open to a process that has
#: stopped, which with no read bound the box listened to for ever.
SSE_READ_TIMEOUT_SECONDS = 60.0


def sse_timeout(connect: float, read: float = SSE_READ_TIMEOUT_SECONDS) -> httpx.Timeout:
    """The event stream's budget: the ordinary call timeout to reach the route,
    and a read bound well past the server's keepalive cadence — long enough
    that a quiet org is never an error, short enough that a dead stream is
    noticed within a minute."""
    return httpx.Timeout(connect, read=read)


logger = logging.getLogger(__name__)

#: What a backend or gateway being replaced answers with while it finishes the
#: requests it already had, and what a load balancer answers with in the gap
#: between the old process closing its listener and the new one binding. None
#: of them is the request's own fault, and all of them are gone a moment later.
CUT_STATUSES = frozenset({502, 503, 504})

#: The transport failures that mean the same thing one layer down: the
#: connection was closed under a request that had been accepted. A connect
#: timeout is NOT here — a backend that cannot be reached at all is an outage
#: the caller's own backoff handles, not a request to replay at once.
CUT_ERRORS = (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)

#: The methods that are safe to send twice by definition. Everything else has
#: to say so at the call site.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _idempotent(method: str) -> bool:
    return method.upper() in IDEMPOTENT_METHODS


def stored_token_reader(boot_token: str) -> Callable[[], str]:
    """A reader of the bearer this machine's credential file holds for the same
    person and org as ``boot_token``, answering ``""`` when there is none.

    Read through a function rather than captured once, because a box outlives
    the token it booted with: a turn may run for days, the box's session is
    re-issued while it does, and a box holding the old string in memory would
    be refused on every request while a good credential sits on its disk. It
    re-reads that one profile, never whichever is current: a sign-in to another
    org on the box's disk can never move a running box into that org.
    """
    from alkera_cli.account.auth_file import profile_matching_token

    def read() -> str:
        profile = profile_matching_token(boot_token)
        return profile.token if profile is not None else ""

    return read


#: The 403 codes that say the session itself was ended, as opposed to one call
#: the caller is not allowed to make.
SESSION_ENDED_CODES = frozenset({"session_revoked"})


class CloudApiError(Exception):
    """The backend answered with an error status."""

    def __init__(self, status: int, body: Any, *, method: str, path: str) -> None:
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        code, message = _code_and_message(body)
        self.code = code
        self.message = message
        super().__init__(f"{method} {path} -> {status}{f' {code}' if code else ''}")

    @property
    def unauthorized(self) -> bool:
        return self.status == 401

    @property
    def session_refused(self) -> bool:
        """The bearer itself was refused (expired, unknown or revoked), not one
        call it may not make. By the time a caller sees this the client has
        already re-read the credential file, so the file holds the same token."""
        return self.status == 401 or (self.status == 403 and self.code in SESSION_ENDED_CODES)


def _code_and_message(body: Any) -> tuple[str, str]:
    """The refusal's code and message: the app's canonical envelope
    ``{"error": {"code", "message"}}`` first — the shape a compute refusal
    (``insufficient_credit``, ``no_compute_grant``, …) arrives in — then
    FastAPI's bare ``detail``, a dict with the same keys or a plain string."""
    if not isinstance(body, dict):
        return "", ""
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or ""), str(error.get("message") or "")
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("code") or ""), str(detail.get("message") or "")
    if isinstance(detail, str):
        return "", detail
    return "", ""


def backend_client(
    api_url: str,
    *,
    timeout: httpx.Timeout | float | None,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: Mapping[str, str] | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """An async client on the backend's API: the one place this package
    builds one, whether its headers are fixed or ``auth`` signs each request."""
    return httpx.AsyncClient(
        base_url=api_url, timeout=timeout, transport=transport, headers=headers, auth=auth
    )


def ws_url_for(api_url: str) -> str:
    """The socket URL for an API base: ``http(s)`` becomes ``ws(s)``."""
    base = api_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base.removeprefix("https://") + WS_PATH
    if base.startswith("http://"):
        return "ws://" + base.removeprefix("http://") + WS_PATH
    raise ValueError(f"api url must be http(s): {api_url!r}")


class CloudRestClient:
    """One authenticated caller. Cheap to clone per chat via :meth:`for_agent`."""

    def __init__(
        self,
        *,
        api_url: str,
        token: str | BoxCredential,
        agent_id: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
        sse_read_timeout: float = SSE_READ_TIMEOUT_SECONDS,
    ) -> None:
        if not api_url.startswith(("http://", "https://")):
            raise ValueError(f"api url must be http(s): {api_url!r}")
        self._api_url = api_url.rstrip("/")
        self._credential = token if isinstance(token, BoxCredential) else BoxCredential(token)
        self._agent_id = agent_id
        self._transport = transport
        # Read from the deployment when the caller names no budget, so a box on
        # a slow link is told once rather than at every construction site.
        self._timeout = rest_timeout_seconds() if timeout is None else timeout
        self._sse_read_timeout = sse_read_timeout
        # Validated eagerly: a bad agent id fails here, not as a 400 per call.
        self._agent_headers = {} if agent_id is None else agent_headers(agent_id)

    @property
    def credential(self) -> BoxCredential:
        """The shared bearer. Refreshing it here refreshes it for every clone."""
        return self._credential

    @property
    def api_url(self) -> str:
        return self._api_url

    @property
    def agent_id(self) -> str | None:
        return self._agent_id

    @property
    def ws_url(self) -> str:
        return ws_url_for(self._api_url)

    def for_agent(self, agent_id: str) -> CloudRestClient:
        """The same credential speaking as another agent (a chat's mirror).

        The credential is shared, not copied: one refresh reaches every chat.
        """
        return CloudRestClient(
            api_url=self._api_url,
            token=self._credential,
            agent_id=agent_id,
            transport=self._transport,
            timeout=self._timeout,
            sse_read_timeout=self._sse_read_timeout,
        )

    def headers(self) -> dict[str, str]:
        return {**bearer_headers(self._credential.token), **self._agent_headers}

    def _client(self, timeout: httpx.Timeout | float | None = None) -> httpx.AsyncClient:
        # A credential that signs each request itself (an org worker's, which
        # can renew an expired bearer) does so; otherwise the header is fixed
        # for the one call this client is built for.
        signer = self._credential.signer()
        return backend_client(
            self._api_url,
            timeout=self._timeout if timeout is None else timeout,
            transport=self._transport,
            headers=dict(self._agent_headers) if signer is not None else self.headers(),
            auth=signer,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        request_timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
        retry_on_cut: bool | None = None,
    ) -> Any:
        """One call; the decoded JSON body (``None`` for an empty one) or a
        :class:`CloudApiError`. Transport errors propagate as ``httpx`` errors.

        ``headers`` are for the one call that needs them (the machine
        credential) — they are never folded into the client's standing headers,
        so a secret one route wants does not ride every other request.

        ``retry_on_cut`` decides whether a request the far side CUT — a
        connection reset, a 502/503/504 from a load balancer or from a backend
        inside its graceful stop — is sent a second time. Once, never more: a
        deploy cuts a request once, and a call that fails again is a real
        failure the caller has to see. The default is the method's own
        idempotence: a GET (or HEAD/OPTIONS) is safe by definition, a POST is
        not, and the POSTs that ARE idempotent say so at the call site rather
        than by a rule that would also cover publishing a message twice.

        A **401** is retried whatever the method, and only when re-reading the
        credential file turned up a different bearer. A box outlives its token:
        a turn may run for days and its session is re-issued while it does, so
        the string it booted with goes stale. A 401 also means the request was
        REFUSED, so nothing happened and replaying it is safe even for a
        publish. If the file says the same thing, the credential really is
        being refused and the error stands.
        """
        retry = _idempotent(method) if retry_on_cut is None else retry_on_cut
        try:
            return await self._one_request(
                method,
                path,
                json_body=json_body,
                params=params,
                request_timeout=request_timeout,
                headers=headers,
            )
        except CloudApiError as exc:
            if exc.unauthorized:
                if not self._credential.refresh():
                    raise
                logger.info(
                    "this box's session had been re-issued; %s %s is sent again with the "
                    "credential on disk",
                    method,
                    path,
                )
            elif not retry or exc.status not in CUT_STATUSES:
                raise
        except CUT_ERRORS:
            if not retry:
                raise
        logger.debug("%s %s was cut; sending it once more", method, path)
        return await self._one_request(
            method,
            path,
            json_body=json_body,
            params=params,
            request_timeout=request_timeout,
            headers=headers,
        )

    async def _one_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        request_timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        async with self._client(request_timeout) as client:
            response = await client.request(
                method, path, json=json_body, params=params, headers=dict(headers or {})
            )
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except json.JSONDecodeError:
                body = response.text
        if response.status_code >= 400:
            raise CloudApiError(response.status_code, body, method=method, path=path)
        return body

    # -- the routes the mirror uses -----------------------------------------

    async def mint_ticket(self) -> str:
        body = await self.request("POST", "/api/v1/ws/tickets")
        return WsTicketResponse.model_validate(body).ticket

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        return _as_dict(await self.request("GET", f"/api/v1/chats/{chat_id}"))

    async def send_admission(self, chat_id: str, *, user_id: str) -> dict[str, Any]:
        """Whether ``user_id`` may still drive ``chat_id`` under the server's
        send rule: ``{"allowed", "code", "message"}``. A server without the
        route answers 404 (:mod:`alkera_cli.cloud.turn_admission`)."""
        return _as_dict(
            await self.request(
                "GET", f"/api/v1/chats/{chat_id}/send-admission", params={"user_id": user_id}
            )
        )

    async def share_scope(self, chat_id: str) -> str | None:
        """Where a "shared" note the agent writes in ``chat_id`` lands: the
        default share scope of the person the chat acts for, or ``None`` when
        they are in no team of the chat's org. Only the chat's publisher may ask."""
        scope = _as_dict(await self.request("GET", f"/api/v1/chats/{chat_id}/share-scope")).get(
            "scope"
        )
        return scope if isinstance(scope, str) and scope else None

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return _as_dict(await self.request("GET", "/api/v1/chats", params=params))

    async def list_chat_messages(
        self, chat_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """One page of a chat's transcript after ``after_seq``.

        The durable record of what was said in a chat, and the only way a
        publisher learns about a message that was relayed while it was not
        subscribed: the relay itself is live-only fan-out.
        """
        return _as_dict(
            await self.request(
                "GET",
                f"/api/v1/chats/{chat_id}/messages",
                params={"after_seq": after_seq, "limit": limit},
            )
        )

    async def files_drive(self) -> dict[str, Any]:
        """The drive this caller reads Files nodes through.

        A chat attachment names a NODE, not a drive, and the content route is
        addressed by both — so the box asks once which drive its credential
        resolves to rather than guessing an id into a URL.
        """
        return _as_dict(await self.request("GET", "/api/v1/files/drives"))

    async def attachment_fetcher(self, *, drive_id: str | None = None) -> RestContentFetcher:
        """A reader for Files content on ``drive_id`` — the chat's drive, off
        its own record — or, for a chat whose record names none, on this
        caller's own drive.

        The drive is the chat's and not the caller's on purpose: a pool box
        serves chats from many orgs, each in its own drive, and a box on its
        machine credential is a member of no org, so asked for "its" drive the
        server answers there is none. Only an org box on its operator's session
        has one to fall back to.

        Built here rather than by the service because the credential and the
        transport are this client's: the fetcher gets the headers to mint with
        and nothing else, and the signed URL it follows carries none of them.
        """
        if drive_id is None:
            drive = await self.files_drive()
            drive_id = str(drive.get("id") or "")
            if not drive_id:
                raise CloudApiError(200, drive, method="GET", path="/api/v1/files/drives")
        return RestContentFetcher(
            api_url=self._api_url,
            headers=self.headers(),
            drive_id=drive_id,
            transport=self._transport,
        )

    async def get_object(self, object_id: str) -> dict[str, Any]:
        return _as_dict(await self.request("GET", f"/api/v1/objects/{object_id}"))

    async def upload_payload(
        self, object_id: str, *, envelope: Mapping[str, Any], receipt: Mapping[str, Any]
    ) -> dict[str, Any]:
        return _as_dict(
            await self.request(
                "POST",
                f"/api/v1/objects/{object_id}/payload",
                json_body={"envelope": dict(envelope), "receipt": dict(receipt)},
            )
        )

    async def fail_payload(self, object_id: str, *, reason: str) -> dict[str, Any]:
        """Say the payload behind a promoted result is not coming, and why.

        The reader who pressed save has usually navigated to the object by the
        time a promote is refused, so a note in the chat reaches nobody: without
        this the object waits in ``pending_upload`` for ever, reading as
        "Saving…" on its own page and as "0 rows" in the list."""
        return _as_dict(
            await self.request(
                "POST",
                f"/api/v1/objects/{object_id}/payload/failed",
                json_body={"reason": reason},
            )
        )

    async def register_machine(
        self,
        *,
        name: str,
        provider: str,
        provider_pod_id: str,
        machine_type_code: str,
        daemon_instance_id: str | None = None,
    ) -> dict[str, Any]:
        """Register the pod this daemon runs on as the org's workspace machine.
        The body is the route's own request model, dumped — never a dict spelled
        here — so a field the route adds or requires fails the build, not the box."""
        body = MachineRegisterRequest(
            provider=provider,
            provider_pod_id=provider_pod_id,
            name=name,
            machine_type_code=machine_type_code,
            daemon_instance_id=daemon_instance_id,
        ).model_dump(mode="json")
        # Idempotent by provider pod id — a second register lands on the same
        # row — so a register cut by a backend being replaced is sent again
        # rather than leaving the box with no machine id and no chats.
        return _as_dict(
            await self.request(
                "POST", "/api/v1/machines/register", json_body=body, retry_on_cut=True
            )
        )

    async def claim_machine(
        self,
        *,
        credential: str,
        name: str,
        provider_pod_id: str,
        capacity: int,
        daemon_version: str,
        daemon_instance_id: str | None = None,
    ) -> dict[str, Any]:
        """Claim the machine a platform credential was minted for.

        The credential rides THIS call's headers only — never the client's
        standing headers — so a box's every other request (the transcript it
        writes, the files it leases) carries its box-user session and nothing
        more. What the box is allowed to say is the route's request model,
        dumped: the instance, the name, what it can hold, which daemon it runs.
        The kind, the size and whom it serves come from the credential.
        """
        return _as_dict(
            await self.request(
                "POST",
                "/api/v1/machines/claim",
                json_body=MachineClaimRequest(
                    provider_pod_id=provider_pod_id,
                    name=name,
                    capacity=capacity,
                    daemon_version=daemon_version,
                    daemon_instance_id=daemon_instance_id,
                    # The box reports the sandbox mode it enforces so the server
                    # can keep a "none" box off the shared multi-tenant pool.
                    sandbox=SandboxSettings.from_env().mode,
                ).model_dump(mode="json"),
                headers=machine_credential_headers(credential),
                # Idempotent by pod id under the credential, exactly as the
                # register above: the same claim twice is the same machine.
                retry_on_cut=True,
            )
        )

    async def heartbeat_machine(
        self,
        machine_id: str,
        *,
        capacity: int | None = None,
        chats_served: int | None = None,
        daemon_version: str | None = None,
        credential: str = "",
        draining: bool | None = None,
        restarting: bool | None = None,
        daemon_instance_id: str | None = None,
        resources: dict[str, Any] | None = None,
        capabilities: list[str] | None = None,
        last_activity_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Stamp the machine's liveness, and say what it is holding. The route
        answers ``204 No Content``, or ``200`` with the machine card for a box
        that backs an org machine; a body, when one comes back, is returned and
        an empty answer is ``{}`` — never an error, or every heartbeat would
        read as the box being unreachable. ``last_activity_at`` is when any chat
        on the box last did work, what an idle stop is measured from.

        The load numbers are how a shared pool is spread: placement puts a new
        chat on the ready box with the most room, and it can only know that
        because the box says so on every beat. A platform box also re-presents
        its credential here, so the beat after an admin revokes it is refused
        rather than carrying on until something else notices. ``draining``
        says the box was told to stop and is finishing what it holds, which is
        what takes it out of placement while it does.

        On its own short budget: a beat that hangs for the default timeout
        delays the next one past the window the cloud judges this box by, so a
        stalled tunnel would make a healthy machine report itself dead.
        """
        body = await self.request(
            "POST",
            f"/api/v1/machines/{machine_id}/heartbeat",
            json_body=MachineHeartbeatRequest(
                capacity=capacity,
                chats_served=chats_served,
                daemon_version=daemon_version,
                draining=draining,
                restarting=restarting,
                daemon_instance_id=daemon_instance_id,
                resources=(
                    MachineResources.model_validate(resources) if resources is not None else None
                ),
                # Re-reported every beat so a box reconfigured and restarted
                # updates the mode its row is judged by.
                sandbox=SandboxSettings.from_env().mode,
                # What this build can do, restated whole: placement keeps a
                # shared workspace's chats on a box that says ``workspaces``,
                # and the server tells a reader a model switch runs on the next
                # turn only on a box that says ``model_switch_v1``.
                capabilities=sorted({*DAEMON_CAPABILITIES, *(capabilities or ())}),
                last_activity_at=last_activity_at,
            ).model_dump(mode="json"),
            request_timeout=heartbeat_timeout_seconds(),
            # A beat is a stamp: sending it twice writes the same moment. A
            # beat lost to a backend being replaced would otherwise spend one
            # of the four the box may miss before it is called unreachable.
            retry_on_cut=True,
            headers=machine_credential_headers(credential) if credential else None,
        )
        return body if isinstance(body, dict) else {}

    async def mint_gateway_token(self, chat_id: str) -> dict[str, Any]:
        """The credential the agent serving ``chat_id`` presents to the gateway:
        ``{"token", "expires_at"}``, minted by the backend for this chat under
        this box's session. The box hands the agent THIS, never its own bearer:
        the gateway bills it as the box's session and the API refuses it.
        Only the chat's publisher (this machine) may ask.

        Idempotent by nature — a second mint is a second token, the first still
        good — so a mint cut by a backend being replaced is sent again.
        """
        return _as_dict(
            await self.request("POST", f"/api/v1/chats/{chat_id}/gateway-token", retry_on_cut=True)
        )

    async def report_publisher_state(
        self,
        chat_id: str,
        *,
        state: str,
        reason: str = "",
        refusal_kind: str | None = None,
        ending: str | None = None,
        workspace_sandbox: str | None = None,
        workspace_memory_mb: int | None = None,
    ) -> dict[str, Any]:
        """Tell the cloud whether this machine can publish ``chat_id``:
        ``refused`` with the gateway's reason, or ``publishing`` again. The
        chat then reads as ``refused`` (and the banner says so) or clears.
        A member of a workspace also says how the workspace's sandbox is;
        a backend that predates workspaces ignores it."""
        return _as_dict(
            await self.request(
                "PUT",
                f"/api/v1/chats/{chat_id}/publisher-state",
                json_body={
                    "state": state,
                    "reason": reason[:500],
                    **({"refusal_kind": refusal_kind} if refusal_kind is not None else {}),
                    **({"ending": ending} if ending is not None else {}),
                    **(
                        {"workspace_sandbox": workspace_sandbox}
                        if workspace_sandbox is not None
                        else {}
                    ),
                    **(
                        {"workspace_memory_mb": workspace_memory_mb}
                        if workspace_memory_mb is not None
                        else {}
                    ),
                },
                # Setting a state twice is setting it once; a lost report
                # leaves the chat reading as it was (a wake still standing).
                retry_on_cut=True,
            )
        )

    async def events(self, *, after: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """The org's invalidation stream as ``{"id", "type", "data"}`` dicts,
        until the server closes it or the caller stops iterating."""
        params = {"after": after} if after is not None else None
        async with (
            self._client(sse_timeout(self._timeout, self._sse_read_timeout)) as client,
            aconnect_sse(client, "GET", "/api/v1/events", params=params) as source,
        ):
            if source.response.status_code >= 400:
                raise CloudApiError(
                    source.response.status_code, None, method="GET", path="/api/v1/events"
                )
            # The stream is up: the one fact a caller cannot read off the frames,
            # since an idle org may send none for minutes.
            yield {"id": None, "type": STREAM_OPENED, "data": None}
            async for event in source.aiter_sse():
                if not event.data:
                    # A comment-only block (``: connected``, ``: keepalive``) or a
                    # bare ``retry:`` line is not a frame.
                    continue
                data: Any = None
                if event.data:
                    try:
                        data = json.loads(event.data)
                    except json.JSONDecodeError:
                        data = event.data
                yield {"id": event.id, "type": event.event, "data": data}


#: The marker :meth:`CloudRestClient.events` yields first, once the server has
#: accepted the stream — before any frame the server itself sent.
STREAM_OPENED = "alkera.stream.opened"


def chat_credential(rest: CloudRestClient, chat_id: str, gateway_token: str) -> FixedCredential:
    """A box-run chat's credential: its own gateway token, and its own door to
    where a "shared" note lands. The box holds no profile of the person the chat
    acts for, and that person's ``/kb/scopes`` refuses the machine, so without
    the door every shared note read as signed out and stayed private."""

    async def share_scope() -> str | None:
        try:
            return await rest.share_scope(chat_id)
        except (CloudApiError, httpx.HTTPError, OSError, ValueError):
            logger.warning("chat %s: its share scope could not be read", chat_id)
            return None

    return FixedCredential(gateway_token, share_scope=share_scope)


def _as_dict(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError(f"expected a JSON object, got {type(body).__name__}")
    return body


__all__ = [
    "SSE_READ_TIMEOUT_SECONDS",
    "STREAM_OPENED",
    "BoxCredential",
    "CloudApiError",
    "CloudRestClient",
    "backend_client",
    "chat_credential",
    "sse_timeout",
    "stored_token_reader",
    "ws_url_for",
]
