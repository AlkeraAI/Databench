"""The production compose example: `.env` is the one place an operator sets
anything, Files is on, and the web image answers the Files content hostname.

The README and deploy/INSTALL.md tell an operator to put settings in the
repository root's `.env`. Compose reads that file only to interpolate `${...}`,
so a setting the compose file does not name never reached a process: the docs
said to set `SSH_MACHINES_ALLOW_PRIVATE_ADDRESSES` and the backend never saw it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from alkera_core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "deploy" / "docker" / "compose.prod.example.yml"
INSTALL = REPO_ROOT / "deploy" / "INSTALL.md"
WEB = REPO_ROOT / "apps" / "web"
APP_SERVICES = ("backend", "worker", "gateway")

pytestmark = pytest.mark.skipif(not COMPOSE.is_file(), reason="no production compose example")

needs_compose = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("docker") is None,
    reason="renders the file with docker compose",
)
needs_envsubst = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("envsubst") is None,
    reason="needs a POSIX sh and envsubst",
)

REQUIRED = {
    "DB_PASSWORD": "x",
    "TEMPORAL_DB_PASSWORD": "y",
    "PUBLIC_BASE_URL": "https://databench.example.com",
    "FILES_CONTENT_BASE_URL": "https://files.databench-content.example",
}


def _setting_env_names() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def _documented_settings() -> list[str]:
    """Every app setting the operator docs name in backticks."""
    names: set[str] = set()
    for doc in (INSTALL,):
        if doc.is_file():
            names |= set(re.findall(r"`([A-Z][A-Z0-9_]{2,})", doc.read_text(encoding="utf-8")))
    return sorted(names & _setting_env_names())


def _render(tmp_path: Path, dotenv: dict[str, str]) -> dict[str, Any]:
    """`docker compose config` on a copy laid out as the repository is, with
    `dotenv` as the root `.env` and nothing from the caller's environment."""
    compose = tmp_path / "deploy" / "docker" / COMPOSE.name
    compose.parent.mkdir(parents=True)
    shutil.copy(COMPOSE, compose)
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in dotenv.items()), encoding="utf-8")
    tool_env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "DOCKER_HOST"}}
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose),
            "--env-file",
            str(env_file),
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        env=tool_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    doc: dict[str, Any] = json.loads(result.stdout)
    return doc


def test_the_docs_name_settings_to_check() -> None:
    assert "SSH_MACHINES_ALLOW_PRIVATE_ADDRESSES" in _documented_settings()


@needs_compose
def test_every_setting_the_docs_name_reaches_every_app_service(tmp_path: Path) -> None:
    documented = _documented_settings()
    marker = {name: f"set-in-dotenv-{index}" for index, name in enumerate(documented)}
    services = _render(tmp_path, {**REQUIRED, **marker})["services"]
    for service in APP_SERVICES:
        environment = services[service]["environment"]
        missing = sorted(name for name, value in marker.items() if environment.get(name) != value)
        assert missing == [], f"{service} never sees {missing} set in .env"


@needs_compose
def test_files_is_on_by_default_on_the_host_volume(tmp_path: Path) -> None:
    doc = _render(tmp_path, REQUIRED)
    backend = doc["services"]["backend"]["environment"]
    assert backend["FILES_ENABLED"] == "true"
    assert backend["FILES_STORE_PROVIDER"] == "filesystem"
    assert backend["FILES_CONTENT_BASE_URL"] == REQUIRED["FILES_CONTENT_BASE_URL"]
    assert (
        doc["services"]["web"]["environment"]["FILES_CONTENT_BASE_URL"]
        == (REQUIRED["FILES_CONTENT_BASE_URL"])
    )
    # The app runs as uid 1000 and a new named volume is root's: the one-shot
    # gives it the Files volume as well as the secrets.
    init = doc["services"]["secrets-init"]
    assert "/var/lib/alkera/files" in " ".join(init["command"])
    assert {v["target"] for v in init["volumes"]} >= {"/var/lib/alkera/files"}


@needs_compose
def test_an_unset_content_origin_is_refused_before_anything_starts(tmp_path: Path) -> None:
    compose = tmp_path / "deploy" / "docker" / COMPOSE.name
    compose.parent.mkdir(parents=True)
    shutil.copy(COMPOSE, compose)
    env_file = tmp_path / ".env"
    unset = {k: v for k, v in REQUIRED.items() if k != "FILES_CONTENT_BASE_URL"}
    env_file.write_text("".join(f"{k}={v}\n" for k, v in unset.items()), encoding="utf-8")
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose), "--env-file", str(env_file), "config"],
        cwd=tmp_path,
        env={k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "DOCKER_HOST"}},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "FILES_CONTENT_BASE_URL" in result.stderr


@needs_compose
def test_files_off_is_an_explicit_choice(tmp_path: Path) -> None:
    doc = _render(tmp_path, {**REQUIRED, "FILES_ENABLED": "false", "FILES_CONTENT_BASE_URL": ""})
    assert doc["services"]["backend"]["environment"]["FILES_ENABLED"] == "false"


def _render_web(tmp_path: Path, **env: str) -> Path:
    templates, conf = tmp_path / "templates", tmp_path / "conf.d"
    templates.mkdir(exist_ok=True)
    conf.mkdir(exist_ok=True)
    shutil.copy(WEB / "nginx.conf.template", templates / "default.conf.template")
    shutil.copy(WEB / "nginx.content.template", templates / "content.server.template")
    base = {
        "PATH": os.environ.get("PATH", ""),
        "ALKERA_BACKEND_URL": "http://backend:8000",
        "ALKERA_NGINX_TEMPLATES": str(templates),
        "ALKERA_NGINX_CONF_DIR": str(conf),
    }
    subprocess.run(
        ["sh", str(WEB / "docker-entrypoint.sh")], env={**base, **env}, check=True, timeout=30
    )
    return conf


@needs_envsubst
@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://files.databench-content.example", id="bare"),
        pytest.param("https://files.databench-content.example:8443/", id="port-and-slash"),
    ],
)
def test_the_web_image_answers_the_content_hostname_with_only_c(
    tmp_path: Path, origin: str
) -> None:
    conf = _render_web(tmp_path, FILES_CONTENT_BASE_URL=origin)
    server = (conf / "files-content.conf").read_text(encoding="utf-8")
    assert "server_name files.databench-content.example;" in server
    assert "location /c/ {" in server
    assert "proxy_pass         http://backend:8000;" in server
    # Nothing but /c on the content hostname reaches the backend.
    assert server.count("proxy_pass") == 1
    assert "return 404;" in server
    assert "${" not in server


@needs_envsubst
def test_without_a_content_origin_there_is_no_content_server(tmp_path: Path) -> None:
    conf = tmp_path / "conf.d"
    conf.mkdir()
    (conf / "files-content.conf").write_text("stale", encoding="utf-8")
    _render_web(tmp_path)
    assert not (conf / "files-content.conf").exists()


@needs_envsubst
def test_the_content_server_loads_after_the_log_format_it_uses(tmp_path: Path) -> None:
    """nginx includes conf.d/*.conf in name order. The content server logs with
    alkera_access, which default.conf defines, so a content file that sorts
    first makes nginx refuse to start on every install with Files on."""
    conf = _render_web(tmp_path, FILES_CONTENT_BASE_URL="https://files.databench-content.example")
    loaded = sorted(path.name for path in conf.glob("*.conf"))
    defined_at = next(
        i
        for i, name in enumerate(loaded)
        if "log_format alkera_access" in (conf / name).read_text(encoding="utf-8")
    )
    used_at = [
        i
        for i, name in enumerate(loaded)
        if "alkera_access"
        in (conf / name).read_text(encoding="utf-8").replace("log_format alkera_access", "")
    ]
    assert used_at and min(used_at) >= defined_at, loaded


def test_every_web_image_carries_the_content_template() -> None:
    for dockerfile in (REPO_ROOT / "Databench" / "apps" / "web" / "Dockerfile", WEB / "Dockerfile"):
        if dockerfile.is_file():
            text = dockerfile.read_text(encoding="utf-8")
            assert "nginx.content.template /etc/nginx/templates/content.server.template" in text
