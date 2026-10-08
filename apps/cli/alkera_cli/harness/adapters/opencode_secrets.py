"""The agent server's secrets: handed to it by file, never by environment.

An environment stays readable in ``/proc/<pid>/environ`` for the life of the
process, by anything running as the same uid, and passes to every child. The
two secrets an agent server is launched with, its loopback password and its
injected config (which carries the chat's gateway token), are written to an
owner-only file in the agent's own state and named in ``ALKERA_SECRETS_FILE``;
the vendored agent reads the file once and removes it (``alkera-secrets.ts``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.harness.adapter import HarnessStartRefusedError

#: Where the agent server is handed its secrets: in its own state, beside the
#: listen file, read once and removed by the agent itself (see
#: :func:`write_agent_secrets`).
SECRETS_FILE = "launch.json"
#: The env var naming that file, as the agent sees it.
AGENT_SECRETS_FILE_VAR = "ALKERA_SECRETS_FILE"
#: The names the agent server reads from its secrets file and nowhere else.
AGENT_AUTH_VAR = "ALKERA_SERVER_PASSWORD"
AGENT_CONFIG_VAR = "ALKERA_CONFIG_CONTENT"


@dataclass(frozen=True, slots=True)
class AgentLaunchEnv:
    """What an agent server is launched with: its environment, and the secrets
    handed to it by file (see :func:`write_agent_secrets`)."""

    env: dict[str, str]
    secrets: dict[str, str]


def agent_secrets(password: str, config: Mapping[str, Any] | None = None) -> dict[str, str]:
    """What the agent server reads from its secrets file: its loopback password
    and, for a chat, its injected config."""
    found = {AGENT_AUTH_VAR: password}
    if config is not None:
        found[AGENT_CONFIG_VAR] = json.dumps(config)
    return found


def launch_env(env: dict[str, str], password: str, config: Mapping[str, Any]) -> AgentLaunchEnv:
    """A chat's agent launch: ``env``, and its password and config by file."""
    return AgentLaunchEnv(env=env, secrets=agent_secrets(password, config))


def write_agent_secrets(directory: Path, secrets_: Mapping[str, str]) -> Path:
    """Write ``secrets_`` to the agent's secrets file in ``directory`` and
    return its path.

    The file is created fresh and owner-only (a stale one, from an agent that
    died before reading it, is removed first) and never through a link: the
    directory is the agent's own state, where it may have left one. A sandbox's
    launch steps then hand it to the chat's uid with the rest of the tree."""
    ChatTree(directory).unlink(SECRETS_FILE)
    body = json.dumps(dict(secrets_)).encode()
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
    if os.open in os.supports_dir_fd:
        parent = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow)
        try:
            fd = os.open(SECRETS_FILE, flags, 0o600, dir_fd=parent)
        finally:
            os.close(parent)
    else:
        fd = os.open(directory / SECRETS_FILE, flags | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
    return directory / SECRETS_FILE


async def require_agent_auth(client: httpx.AsyncClient) -> None:
    """Refuse an agent server that answers a request carrying no password.

    Its API can spawn a PTY and run shell commands, and its authorization is a
    no-op when it holds no password, which is what a binary that does not read
    its secrets file starts with. ``client`` is the adapter's client for the
    server; the request goes without its credentials, and a 401 is the only
    acceptable answer."""
    response = await client.get("/session", auth=None)
    if response.status_code != httpx.codes.UNAUTHORIZED:
        raise HarnessStartRefusedError(
            "the agent server answered a request without its password "
            f"(HTTP {response.status_code}); its binary does not read its secrets file"
        )
