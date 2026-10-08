"""Authentication-related daemon methods.

End-to-end login flow (driven by the editor extension), RFC 8628 device grant:

    extension                   daemon                       backend
    ─────────                   ──────                       ───────
    auth.startDeviceLogin ────► POST /auth/device/code ─────►
                                returns user_code + URLs ◄───
                          ◄──── {user_code, verification_uri, …}
    (opens browser to verification_uri_complete)
                                poll /auth/device/token ─────►  (user approves
                                  authorization_pending ◄────    in the browser
                                  … …                            on any device)
                                  access_token ◄──────────────
                                check (API, email, gateway)
                                write ~/.alkera/auth.yml
                                emit auth.changed (notif) ────►

There is no loopback HTTP server and no paste fallback — the daemon polls the
backend in the background and announces the result via ``auth.changed``. A second
``auth.startDeviceLogin`` (or ``auth.cancelDeviceLogin``) supersedes the first.

Cross-process sync: an ``AuthFileWatcher`` polls ``~/.alkera/auth.yml`` in the
background. When another window (or a terminal ``alkera login``) updates the file,
the daemon re-reads, re-validates, and fires ``auth.changed``; any in-flight
device login here is cancelled since the new state is already on disk.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal

import structlog
from pydantic import BeforeValidator, Field

from alkera_cli.account import auth_file, memberships, orgs
from alkera_cli.daemon.profiles import project_at, require_project_profile
from alkera_cli.daemon.protocol import _DaemonModel, method, notification
from alkera_cli.daemon.server import (
    AuthRequiredError,
    register_shutdown_hook,
    register_startup_hook,
)

if TYPE_CHECKING:
    from alkera_cli.account.auth_watcher import AuthFileWatcher
    from alkera_cli.account.device_flow import DeviceCodeResponse
    from alkera_cli.daemon.server import JsonRpcServer

log = structlog.get_logger(__name__)

AuthReason = Literal["missing", "expired", "invalid", "network", "email_verification_required"]

AuthFailure = Literal["gateway_rejected", "login_failed", "org_refused"]
"""Why a sign-in failed, named more precisely than ``reason`` can. It rides beside a
``reason`` every editor build already settles on (``invalid`` for a gateway
refusal, ``network`` for an unexpected error on this machine), because an editor
that meets a ``reason`` it does not know keeps waiting for approval instead of
showing the sign-in panel again."""

AUTH_DETAIL_MAX_CHARS = 300
"""The longest ``detail`` an auth payload carries. Its source is an exception
message or a gateway's refusal body, neither of which this daemon controls, and
the editor shows it inside one sentence of the sign-in panel."""

_ELLIPSIS = "\u2026"


def cap_auth_detail(value: object) -> object:
    """Truncate a ``detail`` longer than ``AUTH_DETAIL_MAX_CHARS`` to that many
    characters, the last one an ellipsis so the cut is visible. Anything that is
    not a string passes through for the field's own validation to judge."""
    if isinstance(value, str) and len(value) > AUTH_DETAIL_MAX_CHARS:
        return value[: AUTH_DETAIL_MAX_CHARS - len(_ELLIPSIS)] + _ELLIPSIS
    return value


AuthDetail = Annotated[
    str,
    BeforeValidator(cap_auth_detail),
    Field(max_length=AUTH_DETAIL_MAX_CHARS),
]
"""``detail`` as every auth payload types it: capped on construction, so no path
that builds one can send an unbounded string, and ``maxLength`` in the schema."""


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class AuthStatusRequest(_DaemonModel):
    project_path: str | None = None
    """The project to report for: its pin picks the profile. Optional, so an
    older editor that names none gets the profile resolved for the daemon."""


class AuthStatusResponse(_DaemonModel):
    """Current auth state.

    ``reason`` is non-null only when ``authenticated`` is False — it
    drives the editor's LoginPanel subtitle.

    ``frontend_url`` is the config-resolved web-app URL (per-worktree localhost
    in dev, the prod app otherwise). It's a config value, not auth state, but
    it rides this payload because the auth snapshot is the existing seam that
    flows daemon → extension host → webview — the editor's "open web app" button
    reads it from here rather than via a separate RPC.
    """

    authenticated: bool
    email: str | None
    api_url: str | None
    expires_at: datetime | None
    reason: AuthReason | None = None
    frontend_url: str | None = None
    failure: AuthFailure | None = None
    """Set only on a sign-in's terminal failure, alongside ``reason``."""
    detail: AuthDetail | None = None
    """The words behind ``failure``, such as the gateway's own refusal reason."""
    org_team_id: str | None = None
    """The org the reported sign-in acts in."""
    org_name: str | None = None


class AuthStartDeviceLoginRequest(_DaemonModel):
    org_team_id: str | None = None
    """Pre-select this org on the approval page (the approver may pick another;
    the token says which they did)."""


class AuthStartDeviceLoginResponse(_DaemonModel):
    """What the editor displays so the user can approve in a browser.

    The daemon polls the backend in the background; the editor just shows the
    code + URL and waits for an ``auth.changed`` notification."""

    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class AuthCancelDeviceLoginRequest(_DaemonModel):
    pass


class AuthCancelDeviceLoginResponse(_DaemonModel):
    cancelled: bool


class AuthLogoutRequest(_DaemonModel):
    pass


class AuthLogoutResponse(_DaemonModel):
    status: AuthStatusResponse


class AuthListOrgsRequest(_DaemonModel):
    project_path: str | None = None


class AuthOrgInfo(_DaemonModel):
    """One org the person belongs to, and whether this machine holds a sign-in
    for it."""

    org_team_id: str
    org_name: str
    role: str
    sso_required: bool = False
    stored: bool
    current: bool


class AuthListOrgsResponse(_DaemonModel):
    orgs: list[AuthOrgInfo]


class AuthSwitchOrgRequest(_DaemonModel):
    org_team_id: str


class AuthSwitchOrgResponse(_DaemonModel):
    """``switched`` when a stored sign-in for the org became current (then
    ``status`` is the new state); otherwise a device login for the org started
    and ``login`` carries what to show, exactly as ``auth.startDeviceLogin``."""

    switched: bool
    status: AuthStatusResponse | None = None
    login: AuthStartDeviceLoginResponse | None = None


# ---------------------------------------------------------------------------
# Notification: server → client whenever auth state flips
# ---------------------------------------------------------------------------


@notification("auth.changed")
class AuthChangedNotification(_DaemonModel):
    """Fired whenever the daemon's view of auth state changes:
    - successful device login (background poll resolved)
    - logout
    - cross-process update (another window logged in/out, file mtime changed)
    - cloud invalidation detected on a refresh

    The editor extension uses this to update its UI without polling.
    """

    authenticated: bool
    email: str | None = None
    api_url: str | None = None
    expires_at: datetime | None = None
    reason: AuthReason | None = None
    frontend_url: str | None = None
    failure: AuthFailure | None = None
    detail: AuthDetail | None = None
    org_team_id: str | None = None
    org_name: str | None = None


# ---------------------------------------------------------------------------
# Pending-login session state (held on server.pending_login)
# ---------------------------------------------------------------------------


@dataclass
class _DeviceLoginSession:
    """A device login in flight: the device_code being polled, a cooperative
    stop flag the poll thread checks (and sleeps on, so setting it wakes the
    thread at once), the background task driving it, and the flag the poll
    thread sets as it exits, which shutdown waits on."""

    device_code: str
    stop: threading.Event
    task: asyncio.Task[None]
    finished: threading.Event


#: How long daemon shutdown waits for a stopped poll thread to exit. The thread
#: wakes the moment it is stopped; this bounds a token request already in
#: flight, which the device client times out on its own.
_POLL_EXIT_TIMEOUT_S = 15.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _frontend_url() -> str:
    """The config-resolved web-app URL stamped onto every auth payload (so the
    editor's "open web app" button gets the right per-env URL)."""
    from alkera_cli.host.config import get_settings

    return get_settings().alkera_frontend_url.rstrip("/")


def _compute_status(project_path: str | None = None) -> AuthStatusResponse:
    """The sign-in the editor's project acts as, the single source used by
    ``auth.status`` and by the watcher's diff callback. ``project_path`` picks
    the project whose pin decides; None is the profile resolved for the daemon.

    An unverified account reads as signed out, so the login panel links the
    verification page instead of opening chat surfaces the gateway refuses. A
    project pinned to another org than every stored sign-in reads as signed
    out with ``failure="org_refused"`` and the one-line reason in ``detail``
    (``reason`` stays one every editor build already handles)."""
    from alkera_cli.account.login import auth_status

    status = auth_status(project=project_at(project_path))
    stored = status.stored
    if status.reason == "refused":
        return AuthStatusResponse(
            authenticated=False,
            email=None,
            api_url=None,
            expires_at=None,
            reason="invalid",
            frontend_url=_frontend_url(),
            failure="org_refused",
            detail=status.detail,
        )
    return AuthStatusResponse(
        authenticated=status.authenticated,
        email=status.email,
        api_url=stored.api_url if stored is not None else None,
        expires_at=stored.expires_at if stored is not None else None,
        reason=status.reason,
        frontend_url=_frontend_url(),
        org_team_id=(stored.org_team_id or None) if stored is not None else None,
        org_name=(stored.org_name or None) if stored is not None else None,
    )


def _status_project(server: JsonRpcServer) -> str | None:
    """The project the editor last asked status for: what a status the daemon
    pushes on its own (the watcher, a login) reports for."""
    value = getattr(server, "auth_project_path", None)
    return value if isinstance(value, str) and value else None


def _unauthenticated(
    api_url: str | None,
    reason: AuthReason,
    *,
    failure: AuthFailure | None = None,
    detail: str | None = None,
) -> AuthStatusResponse:
    return AuthStatusResponse(
        authenticated=False,
        email=None,
        api_url=api_url,
        expires_at=None,
        reason=reason,
        frontend_url=_frontend_url(),
        failure=failure,
        detail=detail,
    )


def _to_notification(status: AuthStatusResponse) -> AuthChangedNotification:
    return AuthChangedNotification(
        authenticated=status.authenticated,
        email=status.email,
        api_url=status.api_url,
        expires_at=status.expires_at,
        reason=status.reason,
        frontend_url=status.frontend_url,
        failure=status.failure,
        detail=status.detail,
        org_team_id=status.org_team_id,
        org_name=status.org_name,
    )


async def _emit_changed(server: JsonRpcServer, status: AuthStatusResponse) -> None:
    await server.notify("auth.changed", _to_notification(status))


def _cancel_pending(server: JsonRpcServer) -> bool:
    """Tear down any in-flight device login. Returns True if one was active."""
    current = server.pending_login
    if isinstance(current, _DeviceLoginSession):
        current.stop.set()  # cooperative: the poll thread exits within one interval
        current.task.cancel()
        server.pending_login = None
        return True
    return False


async def _stop_pending(server: JsonRpcServer) -> None:
    """:func:`_cancel_pending`, then wait for the poll thread to exit, so no
    poll outlives the daemon and no token request goes out after it stops."""
    current = server.pending_login
    _cancel_pending(server)
    if isinstance(current, _DeviceLoginSession):
        await asyncio.to_thread(current.finished.wait, _POLL_EXIT_TIMEOUT_S)


def _is_current_session(server: JsonRpcServer, device_code: str) -> bool:
    """True if ``device_code`` still owns the pending-login slot (i.e. it hasn't
    been superseded by a newer login or cancelled)."""
    current = server.pending_login
    return isinstance(current, _DeviceLoginSession) and current.device_code == device_code


async def _finish_failure(
    server: JsonRpcServer,
    device_code: str,
    api_url: str,
    reason: AuthReason,
    *,
    failure: AuthFailure | None = None,
    detail: str | None = None,
) -> None:
    """Clear the slot + announce a terminal failure — but only if this login is
    still the active one. A superseding login owns the UI otherwise, so a stale
    poll must stay silent."""
    if _is_current_session(server, device_code):
        server.pending_login = None
        status = _unauthenticated(api_url, reason, failure=failure, detail=detail)
        await _emit_changed(server, status)


# ---------------------------------------------------------------------------
# auth.status
# ---------------------------------------------------------------------------


@method("auth.status")
async def auth_status(server: JsonRpcServer, params: AuthStatusRequest) -> AuthStatusResponse:
    if params.project_path:
        server.auth_project_path = params.project_path  # type: ignore[attr-defined]
    # Run the (potentially-blocking) validation off the loop.
    return await asyncio.get_running_loop().run_in_executor(
        None, _compute_status, params.project_path or _status_project(server)
    )


# ---------------------------------------------------------------------------
# auth.startDeviceLogin
# ---------------------------------------------------------------------------


async def _poll_device(
    server: JsonRpcServer,
    api_url: str,
    device: DeviceCodeResponse,
    stop: threading.Event,
    finished: threading.Event,
) -> None:
    """Background task driving one device login. An unexpected error (writing
    ``auth.yml`` fails, say) ends the login as a failure the editor hears: the
    sign-in panel waits for ``auth.changed`` with no timeout of its own."""
    try:
        await _device_login(server, api_url, device, stop, finished)
    except Exception as exc:
        log.exception("daemon.auth.device_login_failed")
        await _finish_failure(
            server,
            device.device_code,
            api_url,
            "network",
            failure="login_failed",
            detail=str(exc) or type(exc).__name__,
        )


async def _device_login(
    server: JsonRpcServer,
    api_url: str,
    device: DeviceCodeResponse,
    stop: threading.Event,
    finished: threading.Event,
) -> None:
    """Poll the token endpoint (off-loop, since the flow blocks
    on sleeps), then persist + emit ``auth.changed`` on success, or emit a
    failure reason. Stays silent if cancelled / superseded."""
    from alkera_cli.account import device_flow
    from alkera_cli.account.login import complete_login

    loop = asyncio.get_running_loop()

    def _sleep(seconds: float) -> None:
        stop.wait(seconds)  # a stop wakes the thread rather than waiting out the interval

    def _poll() -> str:
        try:
            return device_flow.poll_for_token(
                api_url,
                device.device_code,
                client_id=device_flow.CLIENT_ID_VSCODE,
                interval=device.interval,
                expires_in=device.expires_in,
                should_stop=stop.is_set,
                sleep=_sleep,
            )
        finally:
            finished.set()

    try:
        token = await loop.run_in_executor(None, _poll)
    except device_flow.DeviceLoginCancelledError:
        return  # superseded / cancelled — the canceller owns the slot
    except device_flow.AuthorizationDeniedError:
        await _finish_failure(server, device.device_code, api_url, "invalid")
        return
    except device_flow.DeviceCodeExpiredError:
        await _finish_failure(server, device.device_code, api_url, "expired")
        return
    except device_flow.DeviceFlowError:
        await _finish_failure(server, device.device_code, api_url, "network")
        return
    except asyncio.CancelledError:
        # Drop the slot if it's still ours so a fresh start isn't blocked.
        if _is_current_session(server, device.device_code):
            server.pending_login = None
        raise

    # Re-assert ownership BEFORE touching disk: a login that was superseded /
    # cancelled while its poll was completing must NOT persist a now-stale token.
    if stop.is_set() or not _is_current_session(server, device.device_code):
        return
    # The slot stays ours through the checks, so a cancel, a logout or a newer
    # login that lands during them sets ``stop``, and the library reads it just
    # before the save.
    outcome = await loop.run_in_executor(
        None, lambda: complete_login(api_url, token, should_stop=stop.is_set)
    )
    if outcome.refusal == "cancelled":
        return  # the canceller owns the slot and the UI
    if _is_current_session(server, device.device_code):
        server.pending_login = None
    if outcome.refusal == "email_verification_required":
        # Nothing is saved. The reason lets the login panel link the
        # verification page; once verified, signing in again works.
        await _emit_changed(
            server,
            AuthStatusResponse(
                authenticated=False,
                email=outcome.email,
                api_url=api_url,
                expires_at=None,
                reason="email_verification_required",
                frontend_url=_frontend_url(),
            ),
        )
        return
    if outcome.refusal == "gateway_rejected":
        # The API accepted the token and the gateway refused it. Nothing was saved.
        detail = outcome.gateway.detail if outcome.gateway is not None else None
        status = _unauthenticated(api_url, "invalid", failure="gateway_rejected", detail=detail)
        await _emit_changed(server, status)
        return
    if outcome.refusal is not None:
        # The API rejected the token, and nothing was saved.
        await _emit_changed(server, _unauthenticated(api_url, "invalid"))
        return
    # The new profile is current; what the editor's project acts as is
    # re-derived, since a pinned project may still answer for another org.
    status = await loop.run_in_executor(None, _compute_status, _status_project(server))
    await _emit_changed(server, status)


@method("auth.startDeviceLogin")
async def auth_start_device_login(
    server: JsonRpcServer, params: AuthStartDeviceLoginRequest
) -> AuthStartDeviceLoginResponse:
    """Request a device code from the backend and start polling for approval.

    Single-flight: a second call supersedes the first pending login.
    """
    return await _start_device_login(server, params.org_team_id)


async def _start_device_login(
    server: JsonRpcServer, org_team_id: str | None
) -> AuthStartDeviceLoginResponse:
    from alkera_cli.account import device_flow
    from alkera_cli.host.config import get_settings

    _cancel_pending(server)

    api_url = get_settings().alkera_api_url.rstrip("/")
    loop = asyncio.get_running_loop()
    device = await loop.run_in_executor(
        None,
        lambda: device_flow.request_device_code(api_url, client_id=device_flow.CLIENT_ID_VSCODE),
    )
    approval_url = device_flow.approval_url_for_org(device.verification_uri_complete, org_team_id)

    stop, finished = threading.Event(), threading.Event()
    task = asyncio.create_task(
        _poll_device(server, api_url, device, stop, finished),
        name="alkera-device-login-poll",
    )
    server.pending_login = _DeviceLoginSession(
        device_code=device.device_code, stop=stop, task=task, finished=finished
    )
    return AuthStartDeviceLoginResponse(
        user_code=device.user_code,
        verification_uri=device.verification_uri,
        verification_uri_complete=approval_url,
        expires_in=device.expires_in,
        interval=device.interval,
    )


@method("auth.cancelDeviceLogin")
async def auth_cancel_device_login(
    server: JsonRpcServer, params: AuthCancelDeviceLoginRequest
) -> AuthCancelDeviceLoginResponse:
    """Cancel an in-flight device login (e.g. the user closed the panel)."""
    return AuthCancelDeviceLoginResponse(cancelled=_cancel_pending(server))


# ---------------------------------------------------------------------------
# Cross-process auth.yml watcher — lifecycle hooks
# ---------------------------------------------------------------------------
#
# A second VS Code window (or `alkera login` from a terminal) can change the
# on-disk auth state behind our back. We poll auth.yml every 2s; on a change we
# re-derive status and emit ``auth.changed``. If a device login was in flight
# here, tear it down — the new file already represents the user's intent.


def nudge_cloud_sync(server: JsonRpcServer) -> None:
    """Make every open project's sign-in-following cloud-sync lanes due on the
    next beat.

    Called on any login/logout transition so what those lanes pull reconciles
    within seconds of an auth change instead of waiting a cadence. Gated, not
    forced: a logged-out lane still no-ops cheaply, and a logout wipes nothing
    (the lane treats it as no authoritative answer)."""
    import contextlib

    from alkera_cli.cloud_sync.job import nudge_after_sign_in

    runtimes = dict(getattr(server, "harness_runtimes", {}) or {})
    for runtime in runtimes.values():
        with contextlib.suppress(Exception):
            nudge_after_sign_in(runtime.scheduler())


async def _on_auth_file_changed(server: JsonRpcServer) -> None:
    status = await asyncio.get_running_loop().run_in_executor(
        None, _compute_status, _status_project(server)
    )
    if status.authenticated:
        # Another process completed a login while we were still polling — cancel
        # ours so the background poll thread exits promptly.
        _cancel_pending(server)
    await _emit_changed(server, status)
    nudge_cloud_sync(server)


@register_startup_hook
async def _start_auth_watcher(server: JsonRpcServer) -> None:
    from alkera_cli.account.auth_watcher import AuthFileWatcher
    from alkera_cli.host.paths import AUTH_FILE_PATH

    watcher = AuthFileWatcher(
        AUTH_FILE_PATH,
        on_change=lambda: _on_auth_file_changed(server),
    )
    watcher.start_async()
    # Stash on the server for shutdown access.
    server.auth_watcher = watcher  # type: ignore[attr-defined]


@register_shutdown_hook
async def _stop_auth_watcher(server: JsonRpcServer) -> None:
    watcher: AuthFileWatcher | None = getattr(server, "auth_watcher", None)
    if watcher is not None:
        await watcher.stop()
    await _stop_pending(server)


# ---------------------------------------------------------------------------
# auth.logout
# ---------------------------------------------------------------------------


@method("auth.logout")
async def auth_logout(server: JsonRpcServer, params: AuthLogoutRequest) -> AuthLogoutResponse:
    """Sign out of the current organization. A sign-in to another org stays and
    becomes current; with none left the editor is signed out."""
    # A logout while a login is mid-flight should cancel it.
    _cancel_pending(server)

    loop = asyncio.get_running_loop()
    # Revoke server-side before forgetting the token locally. Off-thread (the
    # SDK is sync) and best-effort — never block logout on a network failure.
    await loop.run_in_executor(None, orgs.sign_out)
    if auth_file.load_profiles() is None:
        status = _unauthenticated(None, "missing")
    else:
        status = await loop.run_in_executor(None, _compute_status, _status_project(server))
    await _emit_changed(server, status)
    return AuthLogoutResponse(status=status)


# ---------------------------------------------------------------------------
# auth.listOrgs / auth.switchOrg
# ---------------------------------------------------------------------------


def _list_orgs(project_path: str | None) -> AuthListOrgsResponse:
    reader = require_project_profile(project_path)
    try:
        listed = orgs.list_orgs(reader)
    except memberships.SignInRejectedError as exc:
        # Only a 401 means the sign-in is gone. Offline, an older server or a
        # 5xx surfaces as an ordinary error and the editor stays signed in.
        raise AuthRequiredError("rejected") from exc
    return AuthListOrgsResponse(
        orgs=[
            AuthOrgInfo(
                org_team_id=row.org_team_id,
                org_name=row.org_name,
                role=row.role,
                sso_required=row.sso_required,
                stored=row.stored,
                current=row.current,
            )
            for row in listed
        ]
    )


@method("auth.listOrgs")
async def auth_list_orgs(
    server: JsonRpcServer, params: AuthListOrgsRequest
) -> AuthListOrgsResponse:
    """The orgs the person belongs to, read as the project's sign-in, with
    which ones this machine holds a sign-in for. Read-only."""
    return await asyncio.get_running_loop().run_in_executor(
        None, _list_orgs, params.project_path or _status_project(server)
    )


@method("auth.switchOrg")
async def auth_switch_org(
    server: JsonRpcServer, params: AuthSwitchOrgRequest
) -> AuthSwitchOrgResponse:
    """Make the org current: its stored sign-in when there is one, else start a
    device login for it. Host-owned (the extension's command and account row),
    never callable from the webview. Running chats keep the sign-in they opened
    with; only new chats in unpinned projects follow the switch."""
    loop = asyncio.get_running_loop()
    if await loop.run_in_executor(None, orgs.switch_org, params.org_team_id) is not None:
        status = await loop.run_in_executor(None, _compute_status, _status_project(server))
        await _emit_changed(server, status)
        nudge_cloud_sync(server)
        return AuthSwitchOrgResponse(switched=True, status=status)
    login = await _start_device_login(server, params.org_team_id)
    return AuthSwitchOrgResponse(switched=False, login=login)


__all__ = [
    "AuthCancelDeviceLoginRequest",
    "AuthCancelDeviceLoginResponse",
    "AuthChangedNotification",
    "AuthFailure",
    "AuthListOrgsRequest",
    "AuthListOrgsResponse",
    "AuthLogoutRequest",
    "AuthLogoutResponse",
    "AuthOrgInfo",
    "AuthReason",
    "AuthStartDeviceLoginRequest",
    "AuthStartDeviceLoginResponse",
    "AuthStatusRequest",
    "AuthStatusResponse",
    "AuthSwitchOrgRequest",
    "AuthSwitchOrgResponse",
]
