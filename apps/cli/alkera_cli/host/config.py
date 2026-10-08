"""CLI configuration with layered precedence.

Resolution order (highest priority first):

1. **Process env vars** — `ALKERA_API_URL`, `ALKERA_FRONTEND_URL`. Power
   users + shell-set overrides + the VSCode extension when it spawns the CLI.
2. **System config** — `/etc/alkera/config.yml` on POSIX,
   `%PROGRAMDATA%\\Alkera\\config.yml` on Windows. Owned by corporate IT /
   MDM. YAML keys: `api_url`, `frontend_url`.
3. **Built-in defaults** — local dev URLs (`http://localhost:8000`,
   `http://localhost:5173`). A connection refused there while nothing above
   chose the URL is answered by `no_server_refusal`, which names
   `ALKERA_API_URL`.

A source checkout additionally overlays its OWN `.env` / `.env.workspace`
(between the system config and the defaults) for per-worktree dev ports. That
layer is deliberately limited to the checkout the running code came from — the
working directory the CLI is invoked in never contributes one, since these URLs
are where the user's bearer token gets sent (see `_dotenv_files`).

The user's `~/.alkera/auth.yml` (written by `alkera login`) supersedes all
of this *for an authenticated session*, since each stored profile carries the
`api_url` its token was issued against. Commands that need to talk to the API
should prefer the resolved profile's `api_url` (`account.binding`) over
`CliSettings` when both are available.

`request_timeout_seconds` stays env-only; not worth layering.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from alkera_cli.host import build_profile, endpoints
from alkera_cli.host.endpoints import (
    DEFAULT_API_URL,
    DEFAULT_FRONTEND_URL,
    DEFAULT_GATEWAY_URL,
    DEFAULT_SENTRY_DSN,
)
from alkera_cli.host.paths import SYSTEM_CONFIG_PATH

_DOTENV_NAMES = (".env", ".env.workspace")


def _looks_like_source_checkout(path: Path) -> bool:
    """Return true for the repo root layout used by source-checkout CLI runs."""
    return (path / "apps" / "cli" / "alkera_cli").is_dir() and (
        path / "packages" / "api-core" / "alkera_core"
    ).is_dir()


def _package_checkout_root() -> Path | None:
    """The Alkera source checkout this module is RUNNING FROM, or ``None``.

    ``<root>/apps/cli/alkera_cli/host/config.py`` → the root is five levels up. An
    installed wheel or a compiled binary has no such layout, so it gets ``None``.
    """
    here = Path(__file__).resolve()
    if len(here.parents) < 5:
        return None
    root = here.parents[4]
    return root if _looks_like_source_checkout(root) else None


def _dotenv_files() -> tuple[str, ...]:
    """The dotenv files CLI settings may read — this checkout's, or none at all.

    `alkera_api_url` / `alkera_gateway_url` decide where the user's bearer token is
    SENT (the gateway catalog call and the harness config both carry it), so the
    directory the CLI happens to be started in must get no vote. pydantic-settings
    resolves a relative dotenv name against the process CWD, and the CLI is
    run from arbitrary — potentially hostile — repositories, so a `.env` committed
    to a cloned repo could otherwise redirect that token to an attacker's host. Only
    the source checkout this module itself lives in is trusted, and only while the
    CWD is inside it: that keeps the dev workflow (the generated `.env.workspace`
    carries this worktree's ports, and `alkera` is commonly run from a subdirectory
    like `apps/cli`) while an installed CLI reads no dotenv at all — its
    URLs come from the process env, the system config, or the built-in defaults, the
    same trusted layers `ci_safe_api_url()` uses.

    A hostile repo cannot buy trust by mimicking the checkout layout either: the root
    must be the one containing the running code, not merely the nearest look-alike
    above the CWD.
    """
    checkout = _package_checkout_root()
    if checkout is None:
        return ()
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        # No readable CWD at all (it was deleted out from under a long-lived
        # process). Settings are loaded on nearly every code path, so this must
        # degrade to "no dotenv layer" rather than take the CLI down — and it is
        # the fail-closed answer anyway: an unreadable CWD cannot be shown to be
        # inside the checkout.
        return ()
    if cwd != checkout and checkout not in cwd.parents:
        return ()
    return tuple(str(checkout / name) for name in _DOTENV_NAMES)


def _read_system_config() -> dict[str, Any]:
    """Load `SYSTEM_CONFIG_PATH` if it exists and is parsable; return {}
    otherwise. Unreadable files are treated as missing — we never block the
    CLI on a malformed system file.
    """
    if not SYSTEM_CONFIG_PATH.exists():
        return {}
    try:
        raw = SYSTEM_CONFIG_PATH.read_text(encoding="utf-8")
        data: Any = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


class CliSettings(BaseSettings):
    """Effective CLI configuration.

    Built by `get_settings()`, which layers env > system file > defaults
    *before* pydantic-settings runs. After that, env vars naturally win
    because pydantic-settings reads them last.
    """

    model_config = SettingsConfigDict(
        # NOTE: `env_file` is deliberately ABSENT here. A value in this literal is
        # computed once, when the class body executes at import — but which dotenv
        # files may be read depends on the CWD (`_dotenv_files`), and a long-lived
        # process (the daemon) changes directories after import. The live decision
        # is therefore made per settings load, in `settings_customise_sources`.
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Rebuild the dotenv layer on EVERY load so the trust rule is re-evaluated.

        `.env.workspace` (gitignored, generated per-worktree) overlays this
        workspace's API/gateway URLs so a plain `uv run alkera …` from the repo root
        targets the right ports. Applied after `.env`; env vars + the system config
        layer still outrank both (see `get_settings()`). Only THIS checkout's files
        are ever read, and only while the CWD is inside it (see `_dotenv_files`) —
        these values decide where a bearer token is sent, so the decision has to
        reflect the CWD of the run being configured, not of whoever imported the
        module first. Source ORDER is pydantic-settings' default: init kwargs > env
        > dotenv > secrets.
        """
        del dotenv_settings  # rebuilt below against the CWD as of right now
        return (
            init_settings,
            env_settings,
            # Only `env_file` is overridden; encoding/case-sensitivity keep coming
            # from `model_config` so the two can't drift apart.
            DotEnvSettingsSource(settings_cls, env_file=_dotenv_files()),
            file_secret_settings,
        )

    # Built-in defaults come from the build profile (dev → localhost, prod →
    # AWS); env + system config still override. See `alkera_cli.host.endpoints`.
    # Read when the settings load (not at import), so a test can point the
    # unconfigured default at a port of its own.
    alkera_api_url: str = Field(default_factory=lambda: endpoints.DEFAULT_API_URL)
    alkera_frontend_url: str = DEFAULT_FRONTEND_URL
    # Local chat traces older than this are deleted at the first chat open of a
    # process (Claude Code's cleanupPeriodDays precedent). 0 disables.
    trace_retention_days: int = 30
    # The model gateway opencode reaches LLM providers through. A separate
    # service from the backend API (its own bind/host), so it has its own URL.
    alkera_gateway_url: str = DEFAULT_GATEWAY_URL
    request_timeout_seconds: float = 5.0
    # Sentry DSN for the CLI + daemon. Defaults to the DSN baked into a prod
    # release binary (None in dev/source builds — a complete no-op). Runtime env
    # (ALKERA_SENTRY_DSN) and `sentry_dsn` in the system config file still win.
    alkera_sentry_dsn: str | None = DEFAULT_SENTRY_DSN
    # Opt-in OTLP export of agent activity to the org's OWN collector (the SIEM
    # path; see otel_export.py). Requires the alkera-cli[otel] extra.
    alkera_otel_enabled: bool = False
    # Collector base URL, e.g. http://localhost:4318. Unset, the exporter
    # follows the standard OTEL_EXPORTER_OTLP_* env vars.
    alkera_otel_endpoint: str | None = None
    # Include statement/argument content (raw commands, tool inputs) in events.
    # Off: metadata only, matching the vendors' redacted defaults.
    alkera_otel_log_content: bool = False
    # Whether this machine's agents may delegate to subagents. Off withholds the
    # agent-spawning tools from every harness backend, drops the delegation
    # guidance from the system prompt, and refuses a call that arrives anyway.
    # A deployment that does not want subagents as a surface — the cloud box that
    # serves the web portal's chats — sets ALKERA_SUBAGENTS_ENABLED=false.
    alkera_subagents_enabled: bool = True


def ci_safe_api_url() -> str:
    """The backend URL for a run holding a CI TOKEN, resolved WITHOUT the repo
    checkout's dotenv files.

    A CI token is a bearer credential. On a ``pull_request`` event GitHub checks
    out the PR's tree, so a force-tracked ``.env`` / ``.env.workspace`` could set
    ``alkera_api_url`` and redirect the token to an attacker's server. ``get_settings``
    reads those dotenv files, so it must NOT decide where a token is sent. Only the
    process env (``ALKERA_API_URL`` -- what the official CI action exports), the
    machine's system config, and the built-in default are trusted here.
    """
    import os

    explicit = os.environ.get("ALKERA_API_URL", "").strip()
    if explicit:
        return explicit
    api = _read_system_config().get("api_url")
    if isinstance(api, str) and api.strip():
        return api.strip()
    return DEFAULT_API_URL


@lru_cache(maxsize=1)
def get_settings() -> CliSettings:
    """Cached accessor with layered precedence: env > system file > defaults.

    pydantic-settings's own order is init-kwargs > env > dotenv > defaults, so
    we deliberately pass the system file's values as kwargs *only when the
    corresponding env var is not set*. That keeps the env var the top
    priority while letting the system file beat the built-in defaults.

    Cached for the process: the dotenv trust decision (`_dotenv_files`) is made
    on the FIRST call, not on import — call `CliSettings()` directly (or
    `get_settings.cache_clear()`) if you need it re-resolved.
    """
    import os

    system = _read_system_config()
    overrides: dict[str, Any] = {}
    if "api_url" in system and not os.environ.get("ALKERA_API_URL"):
        overrides["alkera_api_url"] = system["api_url"]
    if "frontend_url" in system and not os.environ.get("ALKERA_FRONTEND_URL"):
        overrides["alkera_frontend_url"] = system["frontend_url"]
    if "gateway_url" in system and not os.environ.get("ALKERA_GATEWAY_URL"):
        overrides["alkera_gateway_url"] = system["gateway_url"]
    if "sentry_dsn" in system and not os.environ.get("ALKERA_SENTRY_DSN"):
        overrides["alkera_sentry_dsn"] = system["sentry_dsn"]
    return CliSettings(**overrides)


def api_url_configured(settings: CliSettings) -> bool:
    """Whether anything chose the API URL: the env, the system config, this
    checkout's dotenv or a release build's baked endpoint. ``False`` means the
    CLI fell back to the local self-hosted stack."""
    return bool(build_profile.API_URL) or "alkera_api_url" in settings.model_fields_set


def _connect_failure(exc: BaseException) -> httpx.TransportError | None:
    """The connect failure ``exc`` is or was raised from, if any."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, httpx.ConnectError | httpx.ConnectTimeout):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


def no_server_refusal(exc: BaseException, settings: CliSettings) -> str | None:
    """What to tell someone whose CLI has no API URL configured and found
    nothing at the local default, when ``exc`` is that failed connection.
    ``None`` for anything else: a configured URL that is down, another host,
    an error that is not a refused connection."""
    if api_url_configured(settings):
        return None
    failure = _connect_failure(exc)
    if failure is None:
        return None
    try:
        dialed = failure.request.url
    except RuntimeError:
        return None
    default = httpx.URL(settings.alkera_api_url)
    if (dialed.scheme, dialed.host, dialed.port) != (default.scheme, default.host, default.port):
        return None
    return (
        f"No server is configured and nothing answers at {settings.alkera_api_url}. "
        "Set ALKERA_API_URL to your server's address, or start the local stack."
    )
