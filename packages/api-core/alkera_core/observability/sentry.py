"""Sentry integration — gated, redacting, and a complete no-op until a DSN is set.

We're waiting on Sentry credits, so this ships "ready to enable": `init_sentry`
returns immediately when no DSN is configured, and `capture_exception` /
`capture_message` are no-ops until then. Enabling is one env var (`SENTRY_DSN`,
or a per-component override).

`before_send` runs the shared redactor over the event's request/extra/contexts and
strips every stack frame's local variables, so secrets and PII never reach Sentry
even once it's live.
"""

from __future__ import annotations

from typing import Any, Literal, cast

import sentry_sdk

from alkera_core.config import settings
from alkera_core.logging import get_logger
from alkera_core.observability.context import get_trace_id
from alkera_core.observability.redaction import scrub_mapping

log = get_logger(__name__)

# Sentry's accepted log levels (matches sentry_sdk's `capture_message` Literal).
LogLevel = Literal["fatal", "critical", "error", "warning", "info", "debug"]

_initialized = False


def init_sentry(
    component: str,
    *,
    dsn: str | None = None,
    environment: str | None = None,
    traces_sample_rate: float | None = None,
    integrations: list[Any] | None = None,
    **init_kwargs: Any,
) -> bool:
    """Initialize Sentry for `component` (e.g. "backend", "gateway", "daemon").

    No-ops (returns False) when no DSN is configured. `dsn` overrides
    `settings.sentry_dsn` — the CLI/daemon pass a DSN resolved from their own
    config since they don't read the api-core `.env`.
    """
    global _initialized
    resolved_dsn = dsn if dsn is not None else settings.sentry_dsn
    # A self-hosted / on-prem deployment NEVER phones telemetry home to Alkera. The
    # server components (backend / worker / gateway) read api-core settings and pass
    # no explicit `dsn`; we hard-refuse for them even if a DSN somehow reaches the env
    # (baked image, stray secret) — telemetry leaving a customer's VPC would be a
    # data-exfiltration bug. The CLI/daemon pass their OWN `dsn` (a separate channel,
    # their own config) and are exempt from this server-side gate.
    if dsn is None and settings.is_self_hosted:
        log.info("sentry.disabled", component=component, reason="self_hosted")
        return False
    if not resolved_dsn:
        log.debug("sentry.disabled", component=component, reason="no_dsn")
        return False
    if _initialized:
        log.debug("sentry.already_initialized", component=component)
        return True

    env = environment or settings.sentry_environment or settings.app_env
    rate = (
        traces_sample_rate if traces_sample_rate is not None else settings.sentry_traces_sample_rate
    )

    # sentry_sdk loads its auto-enabling integrations by DYNAMIC string import
    # (importlib on names like `sentry_sdk.integrations.aiohttp`). Nuitka's static
    # analysis can't see those, so a frozen binary doesn't bundle them — and
    # loading one at init crashes with ModuleNotFoundError (the field crash: the
    # win32 daemon looping on `No module named 'sentry_sdk.integrations.aiohttp'`).
    # We don't run those frameworks (aiohttp/django/flask/…), so disable the
    # auto-enabling set; the default integrations (logging, excepthook, dedupe, …)
    # stay on and are bundled (the build also --include-package=sentry_sdk).
    # setdefault so a caller can still override.
    init_kwargs.setdefault("auto_enabling_integrations", False)

    # Frame locals are a secret-exfiltration channel: sentry_sdk otherwise ships a
    # repr() of EVERY local in EVERY frame of a captured exception, which on these
    # paths means plaintext login passwords, decrypted warehouse credentials,
    # provider API keys and the CLI's bearer token. sentry_sdk's own EventScrubber
    # matches on the variable NAME only (and non-recursively), so a secret held
    # inside a container-typed local passes straight through it. Nothing here needs
    # frame locals to debug a crash, so they are hard-off — a security control, not
    # a tuning knob. A caller cannot re-enable them (and `_before_send` strips any
    # that reach it anyway, e.g. from a future SDK default).
    init_kwargs.pop("include_local_variables", None)

    sentry_sdk.init(
        dsn=resolved_dsn,
        environment=env,
        release=settings.app_version,
        traces_sample_rate=rate,
        send_default_pii=False,
        include_local_variables=False,
        before_send=_before_send,
        integrations=integrations or [],
        **init_kwargs,
    )
    sentry_sdk.set_tag("component", component)
    sentry_sdk.set_tag("app_env", settings.app_env)
    _initialized = True
    log.info("sentry.initialized", component=component, environment=env)
    return True


def shutdown_sentry() -> None:
    """Flush + close the Sentry client and reset state so a later `init_sentry`
    re-enables it. No-op when not initialized.

    Lets a long-running process (the daemon) STOP reporting the moment telemetry
    is turned off, mid-session, rather than only on the next start.
    """
    global _initialized
    if not _initialized:
        return
    try:
        # `close()` flushes any pending events, then disables the transport.
        sentry_sdk.get_client().close()
    except Exception:  # never let teardown raise into the caller
        log.warning("sentry.shutdown_failed")
    finally:
        # Reset regardless so capture_* no-op and a later init can re-enable.
        _initialized = False
        log.info("sentry.shutdown")


def capture_exception(error: BaseException | None = None, **tags: Any) -> str | None:
    """Send an exception to Sentry (no-op until initialized). Tags the current
    `trace_id` plus any extra tags. Returns the Sentry event id, or None."""
    if not _initialized:
        return None
    with sentry_sdk.new_scope() as scope:
        _tag_scope(scope, tags)
        return sentry_sdk.capture_exception(error)


def capture_message(message: str, *, level: LogLevel = "info", **tags: Any) -> str | None:
    """Send a message to Sentry (no-op until initialized)."""
    if not _initialized:
        return None
    with sentry_sdk.new_scope() as scope:
        _tag_scope(scope, tags)
        return sentry_sdk.capture_message(message, level=level)


def _tag_scope(scope: Any, tags: dict[str, Any]) -> None:
    trace_id = get_trace_id()
    if trace_id:
        scope.set_tag("trace_id", trace_id)
    for key, value in tags.items():
        if value is not None:
            scope.set_tag(key, str(value))


def _before_send(event: Any, hint: Any) -> Any:
    """Redact secrets/PII from the event before it leaves the process.

    Typed loosely (`Any`) so we don't couple to sentry_sdk's TypedDict `Event`
    shape across versions; at runtime the event is a plain dict.
    """
    try:
        data = cast("dict[str, Any]", event)
        _strip_frame_vars(data)
        for key in ("request", "extra", "contexts", "tags", "user"):
            section = data.get(key)
            if isinstance(section, dict):
                data[key] = scrub_mapping(section)
    except Exception:  # never drop an event because scrubbing hiccuped
        log.warning("sentry.before_send_scrub_failed")
    return event


#: Event payloads nest ~6 deep (exception → values → stacktrace → frames → frame);
#: the bound just stops a pathological/cyclic structure from spinning the walk.
_MAX_FRAME_WALK_DEPTH = 12


def _strip_frame_vars(node: Any, depth: int = 0) -> None:
    """Drop every stack frame's `vars` (its local variables) from the event.

    Belt-and-braces for the `include_local_variables=False` init option: whatever
    put frame locals on the event — an SDK default change, a caller-supplied
    integration, a stack attached to a message — they never leave the process.
    Walks generically because frames hang off several sections (`exception`,
    `threads`, a top-level `stacktrace`).
    """
    if depth > _MAX_FRAME_WALK_DEPTH:
        return
    if isinstance(node, dict):
        frames = node.get("frames")
        if isinstance(frames, list):
            for frame in frames:
                if isinstance(frame, dict):
                    frame.pop("vars", None)
        for value in node.values():
            _strip_frame_vars(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _strip_frame_vars(item, depth + 1)
