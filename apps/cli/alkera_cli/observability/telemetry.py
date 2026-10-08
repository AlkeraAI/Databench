"""The unified telemetry opt-out — one source of truth for whether the CLI and
daemon may phone home (the update-check ping + crash/error reports to Sentry).

Telemetry is OFF when ANY of these holds:
  * ``ALKERA_TELEMETRY`` is a falsey token (``0``/``false``/``no``/``off``),
  * the process is a non-interactive CI run (``CI`` env set), so a build pipeline
    never pings or reports, or
  * the ``telemetry_enabled`` preference in ``~/.alkera/preferences.yml`` is false
    (the toggle the TUI Privacy page + the VS Code preferences page write).

Kept deliberately dependency-light (only ``os`` + a lazy import of the prefs
loader) so the daemon can import it on startup cheaply.
"""

from __future__ import annotations

import os

# The falsey vocabulary for ``ALKERA_TELEMETRY`` (mirrors the env opt-out the
# update-check has always honored). Kept narrow on purpose: only an explicit
# off-token disables; anything else (incl. unset) leaves telemetry on.
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


def telemetry_enabled() -> bool:
    """Whether telemetry (update-check ping + crash/error reporting) is allowed."""
    raw = os.environ.get("ALKERA_TELEMETRY")
    if raw is not None and raw.strip().lower() in _FALSE_TOKENS:
        return False
    if os.environ.get("CI"):
        return False
    return not _preferences_disable_telemetry()


def _preferences_disable_telemetry() -> bool:
    """True iff ``~/.alkera/preferences.yml`` opts out of telemetry.

    Read fail-open: a missing / corrupt file means "not disabled".
    """
    try:
        from alkera_cli.preferences.user import load_preferences

        prefs = load_preferences()
    except Exception:
        return False
    return prefs.telemetry_enabled is False


def reconcile_sentry(component: str) -> bool:
    """Enable Sentry for ``component`` iff telemetry is on, disable it otherwise.

    The single gated entry point shared by the CLI crash path and the daemon, so
    the opt-out (env / CI / the ``telemetry_enabled`` preference) governs error
    reporting in ONE place. Idempotent and safe to call repeatedly — on process
    START and whenever the preference TOGGLES — so a long-running process (the
    daemon) starts or stops reporting live, not only on its next start. Returns
    whether Sentry is active afterward (init still no-ops, returning False, when
    no DSN is configured). Lazy imports keep this module dependency-light for the
    daemon's startup path.
    """
    from alkera_core.observability.sentry import init_sentry, shutdown_sentry

    if not telemetry_enabled():
        shutdown_sentry()
        return False

    from alkera_cli.host.config import get_settings

    return init_sentry(component, dsn=get_settings().alkera_sentry_dsn)


__all__ = ["reconcile_sentry", "telemetry_enabled"]
