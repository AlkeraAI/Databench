"""A remote machine's node reaches this deployment's own model gateway on the
app's one public origin, never a gateway baked into its binary.

The settings give a node ``ALKERA_NODE_API_URL`` + ``/gateway`` unless
``ALKERA_NODE_GATEWAY_URL`` names another address; the web image serves that
path from the gateway when ``ALKERA_GATEWAY_UPSTREAM`` names it; and the
production compose wires the two together.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from alkera_core.config import NODE_GATEWAY_PATH, Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB = REPO_ROOT / "apps" / "web"
COMPOSE = REPO_ROOT / "deploy" / "docker" / "compose.prod.example.yml"
#: Where the image puts the gateway template (both web Dockerfiles copy it there).
GATEWAY_TEMPLATE_NAME = "gateway.location.template"
#: The file the entrypoint renders it to, which the main template includes.
GATEWAY_LOCATION_NAME = "alkera-gateway.location"

needs_envsubst = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("envsubst") is None or shutil.which("sh") is None,
    reason="needs a POSIX sh and envsubst",
)


def _settings(**values: str) -> Settings:
    """Settings from ``values`` alone. The process environment is read too, and
    ``make`` exports a worktree's ``.env.workspace``, which names both node
    addresses, so a case that leaves one out must still mean "unset"."""
    unset = {"alkera_node_api_url": "", "alkera_node_gateway_url": ""}
    return Settings(_env_file=None, **{**unset, **values})  # type: ignore[call-arg, arg-type]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        pytest.param(
            {"alkera_node_api_url": "https://databench.example.com"},
            "https://databench.example.com/gateway",
            id="derived-from-the-node-api-url",
        ),
        pytest.param(
            {"alkera_node_api_url": "https://databench.example.com/"},
            "https://databench.example.com/gateway",
            id="no-double-slash",
        ),
        pytest.param(
            {"alkera_node_api_url": "", "frontend_base_url": "https://app.example.com"},
            "https://app.example.com/gateway",
            id="derived-from-the-public-base-url",
        ),
        pytest.param(
            {
                "alkera_node_api_url": "https://databench.example.com",
                "alkera_node_gateway_url": "https://gateway.example.com",
            },
            "https://gateway.example.com",
            id="an-explicit-gateway-wins",
        ),
    ],
)
def test_a_node_is_always_told_this_deployments_gateway(
    values: dict[str, str], expected: str
) -> None:
    assert _settings(**values).node_gateway_url == expected


def _render(tmp_path: Path, **env: str) -> Path:
    templates = tmp_path / "templates"
    conf = tmp_path / "conf.d"
    templates.mkdir(exist_ok=True)
    conf.mkdir(exist_ok=True)
    shutil.copy(WEB / "nginx.conf.template", templates / "default.conf.template")
    shutil.copy(WEB / "nginx.gateway.template", templates / GATEWAY_TEMPLATE_NAME)
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
    "upstream",
    [
        pytest.param("http://gateway:8081", id="bare"),
        pytest.param("http://gateway:8081/", id="trailing-slash"),
    ],
)
def test_the_web_image_serves_the_gateway_at_its_path_when_named(
    tmp_path: Path, upstream: str
) -> None:
    conf = _render(tmp_path, ALKERA_GATEWAY_UPSTREAM=upstream, ALKERA_NGINX_RESOLVER="127.0.0.11")
    location = (conf / GATEWAY_LOCATION_NAME).read_text(encoding="utf-8")
    assert f"location {NODE_GATEWAY_PATH}/ {{" in location
    # The prefix is stripped: /gateway/health/live reaches the gateway as /health/live.
    assert f"rewrite            ^{NODE_GATEWAY_PATH}/(.*)$ /$1 break;" in location
    assert "set                $alkera_gateway http://gateway:8081;" in location
    assert "proxy_buffering         off;" in location
    assert "${" not in location


def _proxy_pass_targets(location: str) -> list[str]:
    return [
        line.split(None, 1)[1].rstrip(";").strip()
        for line in location.splitlines()
        if line.strip().startswith("proxy_pass")
    ]


@needs_envsubst
def test_the_gateway_upstream_is_resolved_per_request_not_at_startup(tmp_path: Path) -> None:
    """nginx resolves a host named in proxy_pass when it starts and refuses to
    start when it cannot, so a gateway that is not up (or not run) would stop the
    whole web server. A variable target is resolved per request through the
    location's resolver instead."""
    conf = _render(
        tmp_path, ALKERA_GATEWAY_UPSTREAM="http://gateway:8081", ALKERA_NGINX_RESOLVER="127.0.0.11"
    )
    location = (conf / GATEWAY_LOCATION_NAME).read_text(encoding="utf-8")
    assert _proxy_pass_targets(location) == ["$alkera_gateway"]
    assert "resolver           127.0.0.11 valid=10s;" in location


@needs_envsubst
@pytest.mark.parametrize(
    ("resolv_conf", "expected"),
    [
        pytest.param("search example\nnameserver 127.0.0.11\n", "127.0.0.11", id="docker-dns"),
        pytest.param(
            "nameserver 10.0.0.2\nnameserver 10.0.0.3\n", "10.0.0.2", id="first-nameserver"
        ),
        pytest.param("nameserver fd00::53\n", "[fd00::53]", id="ipv6-bracketed"),
    ],
)
def test_the_resolver_is_the_containers_own_nameserver(
    tmp_path: Path, resolv_conf: str, expected: str
) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(resolv_conf, encoding="utf-8")
    conf = _render(
        tmp_path, ALKERA_GATEWAY_UPSTREAM="http://gateway:8081", ALKERA_RESOLV_CONF=str(resolv)
    )
    location = (conf / GATEWAY_LOCATION_NAME).read_text(encoding="utf-8")
    assert f"resolver           {expected} valid=10s;" in location


@needs_envsubst
def test_an_explicit_resolver_wins_over_resolv_conf(tmp_path: Path) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 10.0.0.2\n", encoding="utf-8")
    conf = _render(
        tmp_path,
        ALKERA_GATEWAY_UPSTREAM="http://gateway:8081",
        ALKERA_RESOLV_CONF=str(resolv),
        ALKERA_NGINX_RESOLVER="10.9.9.9",
    )
    location = (conf / GATEWAY_LOCATION_NAME).read_text(encoding="utf-8")
    assert "resolver           10.9.9.9 valid=10s;" in location


@needs_envsubst
def test_no_nameserver_refuses_to_render_the_gateway(tmp_path: Path) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("search example\n", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        _render(
            tmp_path, ALKERA_GATEWAY_UPSTREAM="http://gateway:8081", ALKERA_RESOLV_CONF=str(resolv)
        )


@needs_envsubst
@pytest.mark.skipif(shutil.which("nginx") is None, reason="needs nginx")
def test_nginx_accepts_the_location_while_the_gateway_host_does_not_resolve(
    tmp_path: Path,
) -> None:
    conf = _render(
        tmp_path,
        ALKERA_GATEWAY_UPSTREAM="http://no-such-gateway.invalid:8081",
        ALKERA_NGINX_RESOLVER="127.0.0.1",
    )
    main = tmp_path / "nginx.conf"
    main.write_text(
        f"pid {tmp_path / 'nginx.pid'};\nerror_log {tmp_path / 'error.log'};\n"
        "events {}\nhttp {\n  server {\n    listen 127.0.0.1:18089;\n"
        f"    include {conf / GATEWAY_LOCATION_NAME};\n  }}\n}}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["nginx", "-t", "-p", str(tmp_path), "-c", str(main)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@needs_envsubst
def test_without_an_upstream_there_is_no_gateway_route(tmp_path: Path) -> None:
    conf = tmp_path / "conf.d"
    conf.mkdir()
    # A location left by an earlier start with the variable set is removed.
    (conf / GATEWAY_LOCATION_NAME).write_text("stale", encoding="utf-8")
    _render(tmp_path)
    assert not (conf / GATEWAY_LOCATION_NAME).exists()
    assert (conf / "default.conf").is_file()


def test_the_server_block_includes_the_rendered_gateway_location() -> None:
    template = (WEB / "nginx.conf.template").read_text(encoding="utf-8")
    stem, last = GATEWAY_LOCATION_NAME[:-1], GATEWAY_LOCATION_NAME[-1]
    # A pattern, so nginx starts when the location was not rendered.
    assert f"include /etc/nginx/conf.d/{stem}[{last}];" in template


@pytest.mark.parametrize(
    "dockerfile",
    [
        pytest.param(REPO_ROOT / "Databench" / "apps" / "web" / "Dockerfile", id="open"),
        pytest.param(WEB / "Dockerfile", id="product"),
    ],
)
def test_every_web_image_carries_the_gateway_template(dockerfile: Path) -> None:
    text = dockerfile.read_text(encoding="utf-8")
    assert f"nginx.gateway.template /etc/nginx/templates/{GATEWAY_TEMPLATE_NAME}" in text


def _compose_services() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services: dict[str, Any] = doc["services"]
    return services


def test_the_production_compose_routes_the_gateway_through_the_one_origin() -> None:
    services = _compose_services()
    web_env = services["web"]["environment"]
    assert web_env["ALKERA_GATEWAY_UPSTREAM"] == "${ALKERA_GATEWAY_UPSTREAM:-http://gateway:8081}"
    # The gateway publishes no port of its own: the web origin is the only way in.
    assert "ports" not in services["gateway"]
    backend_env = services["backend"]["environment"]
    assert backend_env["ALKERA_NODE_GATEWAY_URL"] == "${ALKERA_NODE_GATEWAY_URL:-}"
