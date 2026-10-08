"""The environment a BOUNDED (cloud) session hands the agent's processes.

A cloud box runs the daemon with everything the daemon needs — the operator's
``ALKERA_HOME``, the box's own bearer, the database it leases, the cloud
provider it was provisioned from — and the agent's shell inherits that
environment unless something takes it away. On a shared box the agent is
assumed prompt-injectable, so what its ``env`` prints must be worth nothing:
this module is the ONE table of names a bounded session never passes on, and
the allowlists each process starts from. The shell tool builds a command's
environment from :data:`BOUNDED_SHELL_ENV_NAMES`; the opencode adapter builds
the harness process's from :data:`BOUNDED_AGENT_ENV_NAMES` and then sets its
own names (its roots, its flags, where its secrets file is).
"""

from __future__ import annotations

from collections.abc import Mapping

#: Exact names a bounded session never hands an agent process.
FORBIDDEN_ENV_NAMES: frozenset[str] = frozenset(
    {
        # The harness's own loopback credentials and injected config.
        "ALKERA_SERVER_PASSWORD",
        "ALKERA_LISTEN_FILE",
        "ALKERA_CONFIG_CONTENT",
        "ALKERA_AUTH_CONTENT",
        "ALKERA_PERMISSION",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_AUTH_CONTENT",
        # Where the operator's credential file lives; the agent has no use for it.
        "ALKERA_HOME",
        "ALKERA_TEST_HOME",
        # The box's account of itself: which allocation it serves, which
        # release it runs, where its binaries are. The agent server reads none
        # of them, and an agent that can print them has a map of the box it is
        # supposed to know nothing about. (The API URL is public and stays.)
        "ALKERA_ALLOCATION_ID",
        "ALKERA_RELEASE_VERSION",
        "ALKERA_OPENCODE_BIN",
        "ALKERA_RIPGREP_BIN",
        # The box's own identity and the machine credential.
        "ALKERA_MACHINE_TOKEN",
        "ALKERA_MACHINE_CREDENTIAL",
        "ALKERA_BOX_TOKEN",
        "ALKERA_GATEWAY_TOKEN",
        "ALKERA_TOKEN",
        "ALKERA_API_TOKEN",
        "ALKERA_CLI_TOKEN",
        # Leased database credentials and the app's own settings.
        "DATABASE_URL",
        "DATABASE_URL_SYNC",
        "PGPASSWORD",
        "PGPASSFILE",
        "JWT_SECRET",
        "SESSION_SECRET",
        "SECRET_KEY",
        "SMTP_PASSWORD",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "FILES_ACCESS_KEY",
        "FILES_SECRET_KEY",
        "FILES_SECRET_ACCESS_KEY",
        # Model providers reached through the gateway, never directly.
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        # Cloud providers the box was provisioned from.
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "RUNPOD_API_KEY",
        "RUNPOD_POD_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_CLIENT_SECRET",
        "DO_API_TOKEN",
        "DIGITALOCEAN_ACCESS_TOKEN",
        "HCLOUD_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        # Names that make an observer run a program: a pager, a preprocessor,
        # a startup file. The shell fence treats a command's own program
        # options as code; these are the same options handed in by name.
        "PAGER",
        "GIT_PAGER",
        "BAT_PAGER",
        "MANPAGER",
        "LESSOPEN",
        "LESSCLOSE",
        "EDITOR",
        "VISUAL",
        "GIT_EXTERNAL_DIFF",
        "GIT_SSH_COMMAND",
        "RSYNC_RSH",
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "PYTHONSTARTUP",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
    }
)

#: Prefixes a bounded session never hands on: any name that spells a secret,
#: token, password or key, and the box's own service configuration.
FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "ALKERA_CLOUD_",
    "ALKERA_MACHINE_",
    "ALKERA_BOX_",
    "ALKERA_GATEWAY_",
    "ALKERA_DISABLE_",
    # How the box sandboxes its chats, supervises its daemon and, on a
    # developer's machine, forwards its ports: the box's business, and a
    # description of the boundary the agent sits inside.
    "ALKERA_SANDBOX_",
    "SANDBOX_",
    "ALKERA_DAEMON_",
    "ALKERA_LOCALDEV_",
    "SNOWFLAKE_",
    "DATABRICKS_",
    "BIGQUERY_",
    "REDSHIFT_",
    "SMTP_",
    "STRIPE_",
    "FILES_",
    "TEMPORAL_",
    "OAUTH_",
    "AWS_",
    "RUNPOD_",
)

#: Substrings that mark a name as a credential whatever its prefix. Read on the
#: upper-cased name.
FORBIDDEN_ENV_MARKERS: tuple[str, ...] = (
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "TOKEN",
    "API_KEY",
    "APIKEY",
    "PRIVATE_KEY",
    "CREDENTIAL",
)

#: Names the agent's shell needs to function, kept even when a marker matches
#: their spelling.
KEPT_ENV_NAMES: frozenset[str] = frozenset({"PATH", "LANG", "LC_ALL", "TERM", "SHELL", "TZ"})


def is_forbidden_env_name(name: str) -> bool:
    """Whether a bounded session withholds ``name`` from the agent."""
    if name in KEPT_ENV_NAMES:
        return False
    if name in FORBIDDEN_ENV_NAMES:
        return True
    upper = name.upper()
    if upper.startswith(FORBIDDEN_ENV_PREFIXES):
        return True
    return any(marker in upper for marker in FORBIDDEN_ENV_MARKERS)


#: The only names a bounded session's SHELL inherits from the daemon's environment.
#: The tables above name what is known to be secret; a box can carry a secret under a
#: name nobody listed (``WAREHOUSE_DSN``, a webhook URL), so the shell a person on a
#: shared box drives starts from what a command needs to run and nothing else.
BOUNDED_SHELL_ENV_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "LANG",
        "LANGUAGE",
        "TERM",
        "COLORTERM",
        "NO_COLOR",
        "TZ",
        "SHELL",
        "USER",
        "LOGNAME",
    }
)

#: Name prefixes a bounded shell inherits (the locale categories).
BOUNDED_SHELL_ENV_PREFIXES: tuple[str, ...] = ("LC_",)


def bounded_shell_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` cut down to :data:`BOUNDED_SHELL_ENV_NAMES` (and the locale prefixes),
    for a command a bounded session runs. A forbidden name stays out even if a future
    edit lists it here."""
    return {
        name: value
        for name, value in env.items()
        if (name in BOUNDED_SHELL_ENV_NAMES or name.startswith(BOUNDED_SHELL_ENV_PREFIXES))
        and not is_forbidden_env_name(name)
    }


#: The names a bounded session's agent SERVER inherits from the daemon's environment:
#: the shell's, its home and temporary directory, and what it needs to reach the model
#: gateway itself (a proxy, a certificate bundle), which no command it runs is handed.
#: Everything else the server is told (its roots, its flags, where its secrets are) the
#: launch sets by name, so a key a box carries under a name nobody listed stays out.
BOUNDED_AGENT_ENV_NAMES: frozenset[str] = BOUNDED_SHELL_ENV_NAMES | frozenset(
    {
        "HOME",
        "TMPDIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)


def bounded_agent_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` cut down to :data:`BOUNDED_AGENT_ENV_NAMES` (and the locale prefixes),
    for the agent server a bounded session spawns. A forbidden name stays out even if
    a future edit lists it here."""
    return {
        name: value
        for name, value in env.items()
        if (name in BOUNDED_AGENT_ENV_NAMES or name.startswith(BOUNDED_SHELL_ENV_PREFIXES))
        and not is_forbidden_env_name(name)
    }


__all__ = [
    "BOUNDED_AGENT_ENV_NAMES",
    "BOUNDED_SHELL_ENV_NAMES",
    "BOUNDED_SHELL_ENV_PREFIXES",
    "FORBIDDEN_ENV_MARKERS",
    "FORBIDDEN_ENV_NAMES",
    "FORBIDDEN_ENV_PREFIXES",
    "KEPT_ENV_NAMES",
    "bounded_agent_env",
    "bounded_shell_env",
    "is_forbidden_env_name",
]
