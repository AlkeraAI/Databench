"""User preferences — persisted to `~/.alkera/preferences.yml`.

A small, per-user settings shape edited by BOTH the CLI (its settings and
slash commands) and the VS Code extension (via the
daemon's `preferences.*` JSON-RPC methods). Because two *different
versions* of the app may read/write the same file, this is a
`VersionedModel`: `extra="allow"` makes an older reader preserve a newer
writer's unknown fields on round-trip, so a concurrent read-modify-write
never clobbers a preference it doesn't know about.

Every field is defaulted so a missing or partially-corrupt file degrades
gracefully to defaults rather than bricking a CLI session.
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

from pydantic import Field, field_validator, model_validator

from alkera_core.versioning import VersionedModel

#: Every stance a harness session can be put in, in ascending order of what the
#: agent may do without asking. This is the vocabulary of
#: ``Preferences.default_permission_mode`` — the ONE preference shared by the
#: TUI, the extension and the browser — so it is spelled here, next to the field,
#: rather than in whichever surface happens to validate a write first. A surface
#: that offers fewer stances (a browser chat offers three) narrows this set; none
#: may widen it.
PERMISSION_MODES: Final[tuple[str, ...]] = (
    "read_only",
    "default",
    "auto",
    "plan",
    "bypass",
)


def _remove_show_thinking(data: dict[str, Any]) -> dict[str, Any]:
    """Retire the second thinking control while preserving every unrelated field."""
    migrated = dict(data)
    migrated.pop("show_thinking", None)
    migrated["schema_version"] = "2.0.0"
    return migrated


class ToolDisclosure(VersionedModel):
    """Whether a tool card shows its body at its two lifecycle points —
    when it spawns and when it finishes. Persisted per tool kind in
    `Preferences.tool_card_disclosure`; an entry overrides the CLI's
    built-in per-kind default for that kind."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    on_spawn: bool = Field(
        default=True,
        description="Show the card's body the moment it first has one (it spawned).",
    )
    on_finish: bool = Field(
        default=True,
        description="Show the card's body once the tool reaches a terminal status.",
    )


class Preferences(VersionedModel):
    """Per-user CLI / extension preferences."""

    SCHEMA_VERSION: ClassVar[str] = "2.0.0"
    MIGRATIONS: ClassVar = {
        version: _remove_show_thinking
        for version in (
            "1.0.0",
            "1.1.0",
            "1.2.0",
            "1.3.0",
            "1.4.0",
            "1.5.0",
            "1.6.0",
            "1.7.0",
        )
    }

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_thinking(cls, data: Any) -> Any:
        """Reject the retired thinking preference even when a current or
        future-schema client sends it as an extra field.

        Named apart from the base class's ``_run_migrations`` so both run: a
        same-named override would replace the version ladder instead of adding
        to it. The two are order-independent, since the ladder's own step drops
        the same key.
        """
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        cleaned.pop("show_thinking", None)
        return cleaned

    theme: str = Field(
        default="",
        description=(
            "The chosen TUI theme name (e.g. 'alkera-dark', 'alkera-slate', "
            "'alkera-light'). Empty is the selectable 'Default' option: the client "
            "auto-detects the terminal background (dark/light) at launch. An "
            "unrecognized name (e.g. a newer build's theme) also renders the "
            "auto-detected default, without being rewritten on disk."
        ),
    )

    show_banner: bool = Field(
        default=True,
        description=(
            "Show the ALKERA wordmark + flask + cloudscape art at the top of "
            "the chat-shell screens. When false, the art collapses but the "
            "session details (and the settings gear) stay visible."
        ),
    )

    reduce_motion: bool = Field(
        default=False,
        description=(
            "Freeze the animated spinners and waves to a static frame "
            "(accessibility / low-power terminals). Animations still tick, "
            "but the frame counter holds, so nothing visibly moves."
        ),
    )

    tool_card_border: bool = Field(
        default=True,
        description=(
            "Draw tool cards with a border (top/bottom frame rules + a left "
            "rail). When false, cards use the borderless 'spine' look — a "
            "type-icon node over a left rail, no frame. Both are legible; this "
            "is a taste toggle under Appearance."
        ),
    )

    default_permission_mode: str = Field(
        default="default",
        description=(
            "Permission mode NEW chats start in. One of: read_only, default, "
            "auto, plan, bypass. The CLI validates / coerces this; an "
            "unrecognized value falls back to 'default'."
        ),
    )

    tool_card_load_policy: str = Field(
        default="use_finished_default",
        description=(
            "Disclosure state applied to tool cards when a conversation is "
            "LOADED from disk (the replay path). One of: all_closed, all_open, "
            "use_spawn_default (the kind's on_spawn), use_finished_default "
            "(the kind's on_finish — today's behavior)."
        ),
    )

    tool_card_disclosure: dict[str, ToolDisclosure] = Field(
        default_factory=dict,
        description=(
            "Per-tool-kind disclosure overrides, keyed by tool kind (e.g. "
            "'bash', 'read', 'write'). An entry overrides the CLI's built-in "
            "default for that kind; a missing key falls through to the "
            "built-in default."
        ),
    )

    model_efforts: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Last reasoning-effort chosen per model id (e.g. "
            "{'claude-opus-4.5': 'high'}). The TUI model picker preselects "
            "the remembered effort when returning to a model, across sessions."
        ),
    )

    telemetry_enabled: bool = Field(
        default=True,
        description=(
            "Send anonymous crash/error reports (Sentry) and update-check pings "
            "from the CLI, daemon, and VS Code extension. Prompts, file contents, "
            "and secrets are NEVER sent. When false, all of it is disabled. The "
            "`ALKERA_TELEMETRY=0` env var (and, in the extension, VS Code's own "
            "telemetry setting) independently disable it too — any one being off "
            "is enough."
        ),
    )

    default_chat_model: str | None = Field(
        default=None,
        description=(
            "Model id a NEW chat seeds with (the TUI top-right label and the VS "
            "Code composer's default selection). Changed only in the Settings "
            "pane; a chat composer's own pick is per-chat and never rewrites this. "
            "None = never set: a new chat opens on the platform default. When the "
            "gateway catalog no longer offers this id, every read falls back to the "
            "platform default and the stale value is cleared (a transient catalog "
            "fetch failure leaves it untouched)."
        ),
    )

    default_chat_effort: str | None = Field(
        default=None,
        description=(
            "Reasoning effort a NEW chat seeds with, paired with "
            "`default_chat_model`. None = the model offers no effort variants "
            "(or never set). When the saved value isn't offered by the default "
            "model, it resets to the model's catalog default effort, falling back "
            "to the middle-most offered effort."
        ),
    )

    sql_statement_timeout_seconds: int = Field(
        default=900,
        description=(
            "Server-side statement timeout (seconds) applied to every connector "
            "SQL query — the engine itself cancels a query that runs longer, so a "
            "runaway read can't bill unbounded warehouse compute. Default 900 (15 "
            "min); 0 disables the timeout (the engine's own default applies). The "
            "`ALKERA_SQL_STATEMENT_TIMEOUT_SECONDS` env var overrides this per "
            "process (MDM / CI). Honored by the session-persistent engines "
            "(Postgres, Redshift, MySQL, Trino, Databricks, Snowflake) and "
            "BigQuery's job timeout; a no-op for local/stateless engines (DuckDB, "
            "SQLite, ClickHouse)."
        ),
    )

    onboarded_cli: bool = Field(
        default=False,
        description=(
            "The user finished (or skipped) the CLI's first-run onboarding flow "
            "on this machine. False shows the flow once at TUI launch; finishing "
            "or skipping sets it. Per-machine, not per-account — a fresh machine "
            "onboards again. The `/onboarding` command resets it to replay."
        ),
    )

    onboarded_extension: bool = Field(
        default=False,
        description=(
            "The user finished (or skipped) the VS Code extension's first-run "
            "onboarding flow on this machine. False shows the flow instead of the "
            "home surface; finishing or skipping sets it. Per-machine, not "
            "per-account. The 'Alkera: Show onboarding tour' command resets it."
        ),
    )

    @field_validator("sql_statement_timeout_seconds", mode="before")
    @classmethod
    def _sane_timeout(cls, value: object) -> object:
        """Degrade a garbage / negative timeout to the 900s default rather than bricking the
        file. Critically this is NOT ``max(0, …)``: a negative or corrupt value must restore
        the protective cap, not silently DISABLE it (only an explicit ``0`` disables the
        timeout) — a money-safety guard that's accidentally turned off is the worst case."""
        try:
            seconds = int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return 900
        return seconds if seconds >= 0 else 900

    @field_validator("default_chat_model", "default_chat_effort", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """A cleared field (empty / whitespace-only string) normalizes to None so
        the file never stores a meaningless `""` for the new-chat default."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


#: The preferences that name something in ONE org's model catalog: a model id,
#: or a per-model memory keyed by model id. A person who belongs to several
#: orgs holds a separate copy of these in each, because a model one org's
#: catalog offers may not exist in another's. Every other field is the
#: identity's own and is shared by every org the person works in.
ORG_SCOPED_KEYS: Final[frozenset[str]] = frozenset(
    {"model_efforts", "default_chat_model", "default_chat_effort"}
)


class DesktopPreferences(Preferences):
    """The desktop's ``~/.alkera/preferences.yml``: the shared document plus a
    copy of the org-scoped keys for every org a sign-in on this machine acts in.

    The top-level org-scoped fields are the file's copy from before it was kept
    per org: an older CLI still reads and writes them there, and a reader that
    knows no org (nothing signed in) reads them. A reader that knows its org
    reads only ``orgs[<org id>]``, never the top-level copy, so a model one
    org's catalog offers never seeds a chat in another org. The server's
    document has no ``orgs``; it keeps the per-org copy in its own table.
    """

    SCHEMA_VERSION: ClassVar[str] = "2.1.0"

    orgs: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "The org-scoped preferences (`model_efforts`, `default_chat_model`, "
            "`default_chat_effort`) per org, keyed by the org's id as the sign-in "
            "names it. Written by the CLI and the daemon only."
        ),
    )


__all__ = [
    "ORG_SCOPED_KEYS",
    "PERMISSION_MODES",
    "DesktopPreferences",
    "Preferences",
    "ToolDisclosure",
]
