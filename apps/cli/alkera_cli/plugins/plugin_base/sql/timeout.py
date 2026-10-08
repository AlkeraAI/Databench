"""Resolve the server-side SQL statement timeout (seconds) a connector applies.

Precedence (highest first): the ``ALKERA_SQL_STATEMENT_TIMEOUT_SECONDS`` env var
(a per-process MDM / CI override) → the user's persisted preference
(``~/.alkera/preferences.yml``) → the 900s (15-min) default the ``Preferences``
model carries. The value caps every connector SQL query SERVER-SIDE so a runaway
read can't bill unbounded warehouse compute; ``0`` disables it.

Read FRESH per query (never cached) so a preference edit in any settings surface
(the TUI modal, the editor's preferences panel) takes effect on the next query
without a daemon restart — the file read is tiny.
"""

from __future__ import annotations

import os

#: The per-process override env var (set by MDM / CI, or the power user's shell).
ENV_VAR = "ALKERA_SQL_STATEMENT_TIMEOUT_SECONDS"


def resolve_sql_statement_timeout_seconds() -> int:
    """The effective statement timeout in seconds (always ``>= 0``; ``0`` = no
    timeout). A malformed env override is ignored (falls through to the preference)
    rather than failing the query."""
    raw = os.environ.get(ENV_VAR)
    if raw is not None:
        try:
            return max(0, int(raw.strip()))
        except ValueError:
            pass  # a malformed override falls through to the persisted preference
    # Lazy import: keeps plugin_base import-light and avoids a load-time dependency
    # on the CLI preferences stack (the same lazy-import pattern the rest of
    # plugin_base uses to reach the context and lineage stores).
    from alkera_cli.preferences.user import load_preferences

    return load_preferences().sql_statement_timeout_seconds


#: Lowercased substrings that a SERVER-SIDE statement/query-timeout cancellation puts in its
#: error message, per engine. Deliberately SPECIFIC (a statement/query timeout, never a bare
#: "timeout"/"timed out" which a connection/network error also says) so the note never
#: mislabels an unrelated failure. Missing one just means no note — the raw engine error still
#: surfaces — so the conservative direction is safe.
_TIMEOUT_SIGNATURES = frozenset(
    {
        "statement timeout",  # postgres/redshift: "canceling statement due to statement timeout"
        "statement_timeout",  # the setting name, if echoed
        "reached its statement timeout",  # snowflake: "...reached its statement timeout of N s"
        "maximum statement execution time",  # mysql 3024: "...maximum statement execution time..."
        "max_execution_time",
        "exceeded maximum time limit",  # trino: "Query exceeded maximum time limit of N"
        "exceeded the maximum execution time",
        "statement execution exceeded",  # databricks-style timeout phrasing
        "job exceeded",  # bigquery jobTimeoutMs
    }
)


#: How a person changes the limit, worded for EVERY surface the daemon serves — a
#: browser chat has no editor preferences panel, so the note names the setting
#: (``Statement timeout``) and who can change it, never one client's menu path.
TIMEOUT_SETTING_HINT = (
    "This limit is a preference: the workspace's Data → Statement timeout setting (in the "
    "chat Settings, or ALKERA_SQL_STATEMENT_TIMEOUT_SECONDS on the machine running the "
    "agent). Ask an admin to raise the query timeout for this workspace, or set it to "
    "'No limit'."
)


def _humanize_timeout(seconds: int) -> str:
    """``900`` → ``"15 minutes"``, ``60`` → ``"1 minute"``, ``90`` → ``"90 seconds"``."""
    if seconds <= 0:
        return "no limit"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} seconds"


def statement_timeout_note(error: object) -> str | None:
    """If ``error`` reads like the engine cancelling a query because it hit the configured
    statement timeout, an agent-facing note explaining the limit is a USER-CHANGEABLE Alkera
    preference (with the current value) — so the model raises it / shrinks the query instead of
    blindly retrying. ``None`` when the error isn't a statement timeout (the raw error stands)."""
    text = str(error).lower()
    if not any(sig in text for sig in _TIMEOUT_SIGNATURES):
        return None
    current = _humanize_timeout(resolve_sql_statement_timeout_seconds())
    return (
        f"The query was cancelled by the SQL statement timeout (currently {current}). "
        f"{TIMEOUT_SETTING_HINT} Do NOT just retry the same query unchanged: make it "
        "cheaper (add filters / a smaller LIMIT / a narrower time range), or ask the user to "
        f"increase the timeout. Engine error: {error}"
    )


__all__ = [
    "ENV_VAR",
    "TIMEOUT_SETTING_HINT",
    "resolve_sql_statement_timeout_seconds",
    "statement_timeout_note",
]
