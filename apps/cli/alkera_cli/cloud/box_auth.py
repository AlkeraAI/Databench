"""Where a box process's credential lives, and how its clients speak with it.

The bearer header is spelled here and nowhere else in box code
(:func:`bearer_headers`). A box's REST clients hold a :class:`BoxCredential`;
an org worker has exactly one source of its current bearer, the
:class:`WorkerCredential` the supervisor mints for (this machine, this org)
and replaces before it expires, over the socketpair. It is never in the
worker's environment or on disk.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable, Generator

import httpx
from alkera_core.machine_refusals import MACHINE_WORKER_CREDENTIAL_EXPIRED

logger = logging.getLogger(__name__)


def bearer_headers(token: str) -> dict[str, str]:
    """The header a request carries its bearer in."""
    return {"Authorization": f"Bearer {token}"}


class BoxCredential:
    """The bearer every client on this box speaks with, and the one place it
    is refreshed.

    Shared BY REFERENCE across :meth:`CloudRestClient.for_agent` clones — a box
    serving twenty chats has twenty clients, and a rotation each of them had to
    notice separately is a box that goes 401 one chat at a time.

    ``read`` is where a fresh bearer comes from, and it is explicit on purpose:
    a client handed a bare string has no credential file behind it and must
    never reach for one, or what a call does on a 401 would depend on whose
    home directory the process happens to be running in.
    """

    def __init__(self, token: str, *, read: Callable[[], str] | None = None) -> None:
        self._token = token
        self._read = read

    @property
    def token(self) -> str:
        return self._token

    def signer(self) -> httpx.Auth | None:
        """Request signing that outranks the fixed header, or ``None``."""
        return None

    def refresh(self) -> bool:
        """Re-read the credential file; ``True`` when it held a DIFFERENT token.

        ``False`` — there is no file behind this one, or it is gone, unreadable,
        or says exactly what this client is already sending — is the answer that
        matters: it means the 401 is the credential itself being refused, not a
        rotation this process slept through, and the caller must treat it as a
        refusal rather than retry the same bearer for ever.
        """
        if self._read is None:
            return False
        fresh = self._read()
        if not fresh or fresh == self._token:
            return False
        self._token = fresh
        return True


#: How long a worker refused for an expired credential waits for the
#: supervisor to hand it a fresh one before the refusal stands.
RENEW_WAIT_SECONDS = 15.0


def credential_expired(response: httpx.Response) -> bool:
    """Whether ``response`` refused the request for a worker credential past
    its life (read only after its body was)."""
    if response.status_code != 401:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    if code is None and isinstance(body, dict) and isinstance(body.get("error"), dict):
        code = body["error"].get("code")
    return code == MACHINE_WORKER_CREDENTIAL_EXPIRED


def _replayable(request: httpx.Request) -> bool:
    """Whether the request's body can be sent a second time: held whole in
    memory, not streamed from a source that is already spent."""
    return isinstance(request.stream, httpx.ByteStream)


class BearerAuth(httpx.Auth):
    """Signs every request with the bearer ``source`` answers at that moment.

    A client built with it holds no bearer of its own: a credential replaced
    while the client lives is the one its next request carries. With a
    ``holder`` that can renew, a request refused for an expired worker
    credential asks for a fresh one once and is sent once more on it; a
    second refusal stands."""

    def __init__(self, source: Callable[[], str], holder: WorkerCredential | None = None) -> None:
        self._source = source
        self._holder = holder

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        sent = self._sign(request)
        response = yield request
        if self._holder is None or response.status_code != 401:
            return
        response.read()
        if credential_expired(response) and _replayable(request):
            if self._holder.renew_blocking(sent):
                self._sign(request)
                yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        sent = self._sign(request)
        response = yield request
        if self._holder is None or response.status_code != 401:
            return
        await response.aread()
        if credential_expired(response) and _replayable(request):
            if await self._holder.renew(sent):
                self._sign(request)
                yield request

    def _sign(self, request: httpx.Request) -> str:
        token = self._source()
        request.headers.update(bearer_headers(token))
        return token


class LiveCredential(BoxCredential):
    """A :class:`BoxCredential` whose bearer is whatever ``source`` answers
    now: a REST client on it never holds a copy that can go stale. ``signer``
    is the request signing its clients use instead of a fixed header."""

    def __init__(self, source: Callable[[], str], signer: httpx.Auth) -> None:
        super().__init__(source())
        self._source = source
        self._signer = signer

    @property
    def token(self) -> str:
        return self._source()

    def refresh(self) -> bool:
        return False

    def signer(self) -> httpx.Auth | None:
        return self._signer


class WorkerCredential:
    """An org worker's one credential: the only source every client the
    worker talks to the backend through reads its bearer from, at each
    request.

    A request refused for an expired credential asks for a fresh one through
    :meth:`renew`, which asks the supervisor once (however many requests were
    refused at the same moment) and waits, bounded, for the frame that
    replaces it."""

    def __init__(self, token: str, *, renew_wait: float = RENEW_WAIT_SECONDS) -> None:
        self._token = token
        self._seq = 0
        self._refused = False
        self._renew_wait = renew_wait
        self._ask: Callable[[], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._renewing: asyncio.Future[None] | None = None

    @property
    def token(self) -> str:
        return self._token

    @property
    def seq(self) -> int:
        """The number of the hand-off that brought the held credential."""
        return self._seq

    @property
    def refused(self) -> bool:
        """Whether the held credential was refused as expired."""
        return self._refused

    def auth(self) -> httpx.Auth:
        """Request signing for an HTTP client of the worker."""
        return BearerAuth(lambda: self._token, holder=self)

    def box_credential(self) -> BoxCredential:
        """The credential a REST client of the worker holds."""
        return LiveCredential(lambda: self._token, self.auth())

    def bind(self, ask: Callable[[], None]) -> None:
        """How to ask the supervisor for a fresh credential (the worker tells
        it what it holds, :attr:`refused` set); called on the worker's event
        loop, which every renewal is then served on."""
        self._ask = ask
        self._loop = asyncio.get_running_loop()

    def replace(self, token: str, *, seq: int = 0) -> None:
        if not token:
            return
        self._seq = max(self._seq, seq)
        if token == self._token:
            return
        self._token = token
        self._refused = False
        if self._renewing is not None and not self._renewing.done():
            self._renewing.set_result(None)
        logger.info("this worker's credential was replaced")

    async def renew(self, refused: str) -> bool:
        """Whether a credential other than ``refused`` is held, after asking
        the supervisor for one when there is none yet."""
        if self._token != refused:
            return True
        if self._ask is None:
            return False
        if self._renewing is None or self._renewing.done():
            self._renewing = asyncio.get_running_loop().create_future()
            logger.info("this worker's credential expired; asking for a fresh one")
            self._refused = True
            self._ask()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._renewing), timeout=self._renew_wait)
        return self._token != refused

    def renew_blocking(self, refused: str) -> bool:
        """:meth:`renew` for a client running off the event loop (the Files
        transfers run in threads). On the loop itself it cannot wait, and the
        refusal stands."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        with contextlib.suppress(RuntimeError):
            if asyncio.get_running_loop() is loop:
                return False
        pending = asyncio.run_coroutine_threadsafe(self.renew(refused), loop)
        try:
            return pending.result(timeout=self._renew_wait + 5.0)
        except (TimeoutError, concurrent.futures.CancelledError):
            return False


__all__ = [
    "RENEW_WAIT_SECONDS",
    "BearerAuth",
    "BoxCredential",
    "LiveCredential",
    "WorkerCredential",
    "bearer_headers",
    "credential_expired",
]
