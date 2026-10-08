"""The open platform's FastAPI app factory.

``create_app`` builds the app from the routers every deployment serves plus
whatever the installed extensions registered. It installs nothing itself: a
distribution's composition root installs its extensions first, and a process
that calls this directly gets the open platform alone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from functools import cache

from alkera_core.auth.tenancy import ORG_HEADER
from alkera_core.brand import product_name
from alkera_core.config import settings
from alkera_core.entitlements import log_entitlements_status
from alkera_core.http import log_egress_status
from alkera_core.logging import configure_logging, get_logger
from alkera_core.observability.asgi import setup_observability
from alkera_core.validation.storable_text import SelfValidated
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import rate_limit
from backend.api.admin import build_admin_router
from backend.api.body_limit import GateIngestBodyLimitMiddleware
from backend.api.csrf import CsrfOriginMiddleware
from backend.api.extension_points import (
    LIFESPANS,
    SELF_VALIDATED,
    MountPoint,
    RouterSlot,
    mounts_at,
    routers_for,
    signed_bodies,
)
from backend.api.org_echo import OrgEchoMiddleware
from backend.api.routes import notebooks
from backend.api.routes.audit import org as org_audit
from backend.api.routes.audit import security_events
from backend.api.routes.charts import export as chart_export
from backend.api.routes.chats import chats
from backend.api.routes.chats import read_state as chat_read_state
from backend.api.routes.chats import templates as chat_templates
from backend.api.routes.chats import wake as chat_wake
from backend.api.routes.chats import workspace as chat_workspace
from backend.api.routes.compute import box_logs as box_logs_routes
from backend.api.routes.compute import compute, machines
from backend.api.routes.compute import node_bundle as node_bundle_routes
from backend.api.routes.compute import notices as compute_notices
from backend.api.routes.compute import org_machines as org_machines_routes
from backend.api.routes.compute import personal_boxes as personal_boxes_routes
from backend.api.routes.connections import held, team_connections
from backend.api.routes.files import (
    SELF_VALIDATED as FILES_SELF_VALIDATED,
)
from backend.api.routes.files import (
    build_files_router,
    register_files_error_handlers,
    require_files_enabled,
)
from backend.api.routes.identity import account as account_lifecycle
from backend.api.routes.identity import (
    auth,
    device_auth,
    oauth,
    org_sessions,
    scim,
    sso,
    users,
)
from backend.api.routes.infra import config, health
from backend.api.routes.objects import objects
from backend.api.routes.ops import deployment as org_deployment
from backend.api.routes.ops import errors
from backend.api.routes.org import dashboard, invitations, memberships, model_providers, teams
from backend.api.routes.org import members as org_members
from backend.api.routes.org import orgs as own_orgs
from backend.api.routes.org import preferences as me_preferences
from backend.api.routes.org import settings as org_settings
from backend.api.routes.realtime import events, ws
from backend.api.routes.workspaces import machine as workspace_machine
from backend.api.routes.workspaces import workspaces
from backend.auth.dependencies import (
    require_verified_or_grace,
    require_verified_or_grace_or_machine,
)
from backend.authz import decide_on_record
from backend.content_app import CONTENT_MOUNT_PATH, build_content_app
from backend.services.compute import start_local_box_keepalive, stop_local_box_keepalive
from backend.services.identity import ScimError
from backend.services.realtime import runtime as realtime_runtime


async def _row_security_canary_at_boot() -> None:
    """Ask the tenant-isolation canary once as the server starts, so a
    database that cannot be trusted is named in the boot log. The server
    still starts — readiness re-asks and keeps the task out of rotation
    until the answer is clean — because a boot that died here would hide the
    reason behind a restart loop."""
    from alkera_core.db.session import AsyncSessionLocal

    from backend.services.infra import row_security_canary

    try:
        async with AsyncSessionLocal() as db:
            await row_security_canary.check(db)
    except Exception as exc:
        get_logger(__name__).error(
            "row_security.canary.unanswered", error=str(exc) or type(exc).__name__
        )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """What lives as long as the server: the realtime runtime — the outbox
    listener and the hub the event stream fans out from — and whatever the
    installed extensions keep running beside it (``LIFESPANS``; Slack's relay
    sweeper is one). Only a server runs this — ``app = create_app()`` at import
    (the OpenAPI export, an in-process test client) opens no connection and
    starts no task.

    In a local deployment it also keeps the developer's local boxes running
    (``backend.services.compute.start_local_box_keepalive``).

    Everything is stopped on the way out, in the reverse of the order it
    started, so a reload or a rolling deploy leaves nothing listening or
    polling for a process that is going away.
    """
    # A server that would sign sessions with a published dev secret never starts.
    settings.require_server_secrets()
    await _row_security_canary_at_boot()
    runtime = await realtime_runtime.start(application, decide=decide_on_record)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(realtime_runtime.stop, application, runtime)
        for extension_lifespan in LIFESPANS.items():
            await stack.enter_async_context(extension_lifespan(application))
        box_keepalive = start_local_box_keepalive()
        stack.push_async_callback(stop_local_box_keepalive, box_keepalive)
        yield


def create_app() -> FastAPI:
    configure_logging()
    log = get_logger(__name__)
    log_entitlements_status("backend")
    log_egress_status("backend")

    # Interactive API docs (Swagger/ReDoc) + the raw OpenAPI route are exposed
    # only in local dev; in staging/production they're disabled to shrink the
    # public attack surface (schema enumeration). `make gen-openapi` is
    # unaffected — it calls `app.openapi()` in-process, not the HTTP route.
    docs_enabled = settings.is_local and settings.app_env_is_explicit
    application = FastAPI(
        title=f"{product_name()} API",
        version="0.0.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
        # Every HTTP route is throttled by the class it resolves to, before any
        # session is opened: the app-level dependency runs first on every route.
        dependencies=[Depends(rate_limit.enforce_rate_limit)],
    )

    # Bound the gate CI bodies from Content-Length BEFORE FastAPI buffers
    # them: the edge WAF exempts these paths from its 8 KB body rule, and
    # request parsing reads the whole payload ahead of the CI-token auth check.
    # Added first so it sits innermost — the trace-context middleware below has
    # already bound a trace_id for its error envelope.
    # An extension's signed paths (Slack's events) are bounded here too.
    application.add_middleware(
        GateIngestBodyLimitMiddleware,
        max_bytes=settings.gate_ingest_max_body_bytes,
        signed_bodies=signed_bodies(),
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # The portal compares the org a response was answered in with the org
        # it rendered; a cross-origin reader must be allowed to see it.
        expose_headers=[ORG_HEADER],
    )
    # Every response that resolved a credential names its org.
    application.add_middleware(OrgEchoMiddleware)

    # CSRF: a state change carried by the session cookie must also name an
    # origin this deployment serves the portal from. Added after CORS so it sits
    # OUTSIDE it — a forged request is refused without ever being told, through
    # an Access-Control-Allow-Origin header, which origins would have worked.
    # A preflight OPTIONS is a safe method and passes straight through to CORS.
    application.add_middleware(CsrfOriginMiddleware)

    # Sentry (gated on DSN) + per-request trace context / access logging + the
    # canonical error-envelope exception handlers. Added after CORS so the
    # context middleware is outermost (every response — including errors — gets
    # an X-Trace-Id, and CORS headers still apply on the way out).
    setup_observability(
        application,
        component="backend",
        # Some surfaces answer a null byte better than a generic refusal
        # could, so the boundary scan defers to them on what each declares:
        #   • Files, on the parts of a request it names beside its router (its
        #     path ids, the names it validates, what a box reports from its
        #     own filesystem); the rest of a Files request is scanned;
        #   • the content plane, on everything: it answers every lexical
        #     refusal with one opaque 404, so that a path which cannot resolve
        #     tells a caller nothing about what does exist;
        #   • whatever an extension registers in SELF_VALIDATED.
        # What a surface misses still meets the driver-level refusal.
        self_validated=(
            FILES_SELF_VALIDATED,
            SelfValidated.whole(CONTENT_MOUNT_PATH),
            *SELF_VALIDATED.items(),
        ),
    )

    # Email-verification gate: product routers refuse a blocked (unverified past
    # grace) account. The health / auth / oauth routers stay OPEN so a blocked
    # user can still read `/auth/me`, resend the link, and log out — the minimal
    # surface the SPA's full-screen verify gate needs.
    gated = [Depends(require_verified_or_grace)]
    # The two routers a box calls on its own machine credential: the gate lets
    # the machine principal through to the route's own decision and is the
    # same gate as above for everyone else.
    gated_or_machine = [Depends(require_verified_or_grace_or_machine)]
    # What each slot's gate is when an extension's router is included.
    slot_dependencies = {
        RouterSlot.PUBLIC: [],
        RouterSlot.GATED: gated,
        RouterSlot.GATED_OR_MACHINE: gated_or_machine,
        RouterSlot.WEBHOOK: [Depends(rate_limit.limited("webhook"))],
        RouterSlot.MACHINE: [*gated_or_machine, Depends(rate_limit.limited("chat"))],
    }

    def include_registered(point: MountPoint) -> None:
        for mount in mounts_at(point):
            application.include_router(mount.router, dependencies=slot_dependencies[mount.slot])

    application.include_router(health.router)
    application.include_router(config.router)  # public — branding for the pre-auth SPA
    application.include_router(auth.router)
    application.include_router(org_sessions.router)
    # Device Authorization Grant (RFC 8628): the CLI/daemon endpoints are public
    # (no user yet) and the SPA consent endpoints self-gate via `CurrentUser`, so
    # the whole router stays OPEN like auth/oauth.
    application.include_router(device_auth.router)
    application.include_router(oauth.router)
    # SSO login/callback/discover are PUBLIC (no session yet) — like oauth.
    application.include_router(sso.router)
    application.include_router(sso.link_router)
    # Crash reports + client-error events stay OPEN (authed per-route) so a
    # blocked / erroring client can always reach them — like auth/health/oauth.
    application.include_router(errors.router)
    # The person's own deletion: open platform, mounted OPEN like auth, because
    # the right to erasure does not wait on a verified email. An extension's
    # part of the account lifecycle (the data export) mounts beside it.
    application.include_router(account_lifecycle.router)
    include_registered(MountPoint.ACCOUNT_LIFECYCLE)
    # Extension webhooks (billing's Stripe events, the GitHub App): public,
    # trusted by signature only, under the webhook rate-limit class.
    include_registered(MountPoint.WEBHOOKS)
    # A provider's shutdown notice: same trust model (public, signature-only).
    application.include_router(
        compute_notices.router, dependencies=[Depends(rate_limit.limited("webhook"))]
    )
    # Extension routers that authenticate their own callers (Slack's events and
    # interactivity are signature-only, like the webhooks above).
    include_registered(MountPoint.PUBLIC)
    application.include_router(org_settings.router, dependencies=gated)
    # The org's extension surfaces: its Slack settings, billing, storage,
    # allocations and usage.
    include_registered(MountPoint.ORG)
    application.include_router(org_members.router, dependencies=gated)
    application.include_router(sso.org_router, dependencies=gated)
    application.include_router(model_providers.router, dependencies=gated)
    application.include_router(org_deployment.router, dependencies=gated)
    application.include_router(org_audit.router, dependencies=gated)
    application.include_router(security_events.router)
    include_registered(MountPoint.KNOWLEDGE)
    application.include_router(users.router, dependencies=gated)
    application.include_router(teams.router, dependencies=gated)
    # Team and personal connections, their leases and the connector forms,
    # ahead of what an extension layers on them (checks, sign-in, inventory).
    application.include_router(team_connections.router, dependencies=gated)
    include_registered(MountPoint.CONNECTIONS)
    application.include_router(memberships.router, dependencies=gated)
    application.include_router(own_orgs.router, dependencies=gated)
    application.include_router(invitations.team_invitations_router, dependencies=gated)
    # `my_invitations_router` carries the PUBLIC invite-preview route
    # (`/invitations/by-token/{token}`), so it can't be blanket-gated — its
    # authed routes gate themselves via `VerifiedUser` instead.
    application.include_router(invitations.my_invitations_router)
    application.include_router(dashboard.router, dependencies=gated)
    # The compute plane: the catalog + session allocations, and the org's workspace
    # machine (registered and heartbeated by the daemon acting for the user).
    application.include_router(compute.router, dependencies=gated)
    # The machines an org holds and assigns, and its compute settings.
    application.include_router(org_machines_routes.router, dependencies=gated)
    # What an extension sells beside them: offerings, prices and purchases.
    include_registered(MountPoint.ORG_MACHINES)
    # The boxes on a person's own hardware: listed and revoked by that person.
    application.include_router(personal_boxes_routes.router, dependencies=gated)
    application.include_router(
        machines.router,
        dependencies=[*gated_or_machine, Depends(rate_limit.limited("machine"))],
    )
    # A node fetches the bundle its daemon is installed from; the route checks
    # the machine credential itself, since a node fetches before it has claimed.
    application.include_router(
        node_bundle_routes.router, dependencies=[Depends(rate_limit.limited("machine"))]
    )
    # A box posts its supervisor's events on its machine credential.
    application.include_router(
        box_logs_routes.router,
        dependencies=[*gated_or_machine, Depends(rate_limit.limited("machine"))],
    )
    application.include_router(
        chats.router, dependencies=[*gated_or_machine, Depends(rate_limit.limited("chat"))]
    )
    # The one product router that carries its own verification gate instead of
    # taking `gated`: a token is refused here with "this surface is for a
    # person", which a router-level gate would pre-empt with a bare 401.
    application.include_router(
        chat_workspace.router, dependencies=[Depends(rate_limit.limited("chat"))]
    )
    application.include_router(
        chat_templates.router, dependencies=[*gated, Depends(rate_limit.limited("chat"))]
    )
    application.include_router(
        chat_wake.router, dependencies=[*gated, Depends(rate_limit.limited("chat"))]
    )
    application.include_router(
        chat_wake.workspace_router, dependencies=[*gated, Depends(rate_limit.limited("chat"))]
    )
    application.include_router(
        chat_read_state.router, dependencies=[*gated, Depends(rate_limit.limited("chat"))]
    )
    application.include_router(
        chat_read_state.workspace_router,
        dependencies=[*gated, Depends(rate_limit.limited("chat"))],
    )
    application.include_router(
        workspaces.router, dependencies=[*gated, Depends(rate_limit.limited("chat"))]
    )
    application.include_router(
        workspace_machine.router, dependencies=[*gated, Depends(rate_limit.limited("chat"))]
    )
    # Doors a box calls on its own machine credential for what it holds (the
    # connections of a chat bound to it, of a workspace it holds), then an
    # extension's own.
    for router in (held.workspaces_router, held.chats_router):
        application.include_router(router, dependencies=slot_dependencies[RouterSlot.MACHINE])
    include_registered(MountPoint.HELD)
    # A box on its own machine credential reads and fills the results of
    # the chats bound to it: the gate admits the machine, and each route
    # decides whether it may be there (most still want a person).
    application.include_router(objects.router, dependencies=gated_or_machine)
    # Chart export renders the rows it is sent and stores nothing; an agent on
    # its box's machine credential is its main caller, so the machine passes the
    # gate. Rendering is CPU, so it is rate limited per principal.
    application.include_router(
        chart_export.router,
        dependencies=[*gated_or_machine, Depends(rate_limit.limited("mutation"))],
    )
    # The invalidation stream is a product surface like the dashboard it feeds:
    # a blocked account gets the same 403 here as everywhere else.
    application.include_router(
        events.router,
        dependencies=[*gated_or_machine, Depends(rate_limit.limited("stream_open"))],
    )
    # Socket tickets are minted over an ordinary gated request; the socket
    # itself authenticates by hand (no dependency runs on a WebSocket scope)
    # and re-runs the gate's checks inside the handshake and on every tick. A
    # box on its own machine credential mints one too — its ticket is bound to
    # the credential — so the gate admits the machine to the route, which
    # decides for itself which ticket to mint.
    application.include_router(ws.ticket_router, dependencies=gated_or_machine)
    application.include_router(ws.socket_router)
    # The reader's own extension surfaces (their credits and storage).
    include_registered(MountPoint.ME)
    # The reader's own chat settings — the model catalog, the new-chat seed and
    # the preferences behind them. Self-scoped: the caller's identity IS the
    # authorization, so there is no policy, only the gate every product surface
    # is behind.
    application.include_router(me_preferences.router, dependencies=gated)
    include_registered(MountPoint.ACCOUNT)
    application.include_router(
        build_admin_router(routers_for(RouterSlot.ADMIN)),
        dependencies=[*gated, Depends(rate_limit.limited("admin"))],
    )
    # SCIM is machine-to-machine (bearer-authed), NOT a cookie session, so it's
    # NOT behind the email-verification gate. Its own bearer dep scopes the org.
    application.include_router(scim.router)
    # Extension surfaces a CI system reaches (the gate's token management, its
    # self-authenticating ingest, waivers and the portal).
    include_registered(MountPoint.CI)

    # Files is always mounted so the OpenAPI surface — and therefore the
    # generated SDKs — carry it even while production ships dark; the router's
    # own `require_files_enabled` answers the opaque 404 when the flag is off.
    #
    # The kill switch is listed AHEAD of the verification gate, and so is
    # decided before any credential is read: a dark deployment answers the same
    # opaque 404 to everyone. Behind `gated` it would answer a 401 to an
    # anonymous prober and a 403 `email_verification_required` to an account
    # whose grace window lapsed — either one an oracle that the surface exists,
    # and both of them a different answer for the same dark route depending on
    # who happened to ask.
    # A box on its own machine credential reaches the folders of the chats
    # bound to it: the gate admits the machine, and the Files context and
    # policy decide which drive and which nodes it holds.
    application.include_router(
        build_files_router(), dependencies=[Depends(require_files_enabled), *gated_or_machine]
    )
    register_files_error_handlers(application)
    # Notebooks name their drive and node like the Files routes and are dark
    # with them; a box reaches them on its own credential (its peer ops and
    # its kernel's events), decided by the Files policy and the lease fence.
    application.include_router(
        notebooks.router, dependencies=[Depends(require_files_enabled), *gated_or_machine]
    )
    # User bytes are served by a second app with its own middleware stack, and
    # only under the content hostname: a session cookie must never travel on the
    # origin that serves content someone else uploaded.
    application.mount(CONTENT_MOUNT_PATH, build_content_app())

    # SCIM failures must render the RFC 7644 error shape (+ scim+json), not the
    # app's canonical {error:{...}} envelope, so IdPs parse them.
    @application.exception_handler(ScimError)
    async def _scim_error(_request: object, exc: ScimError) -> object:
        return scim.scim_error_response(exc)

    # Pin every route to its rate-limit class now that the last router is in.
    rate_limit.install(application)

    log.info("app.startup", env=settings.app_env, cors=settings.cors_origins_list)
    return application


@cache
def process_app() -> FastAPI:
    """This process's one app, built on first call from the extensions installed
    by then. A distribution's composition root and a test suite both name it, so
    a process that reaches the app two ways holds a single instance."""
    return create_app()
