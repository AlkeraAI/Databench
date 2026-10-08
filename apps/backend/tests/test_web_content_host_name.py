"""The web image starts whatever the Files content hostname is.

nginx hashes every ``server_name`` into buckets the size of the CPU cache line
by default (often 64 bytes); a name that does not fit stops nginx at start
with "could not build server_names_hash". The content origin's server is named
after ``FILES_CONTENT_BASE_URL``'s host, and real ones are long: a tunnel name,
a cloud load balancer's. One such host took a deployment's web container down.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB = REPO_ROOT / "apps" / "web"
#: The longest name DNS allows.
LONGEST_DNS_NAME = 253
#: A real content hostname of the kind that stopped the web container.
LONG_HOST = "theater-beast-break-shareware.trycloudflare.com"
#: A name at DNS's length limit: three 63-character labels and one of 61.
LIMIT_HOST = ".".join(["a" * 63] * 3 + ["b" * 61])

needs_envsubst = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("envsubst") is None or shutil.which("sh") is None,
    reason="needs a POSIX sh and envsubst",
)


def test_the_hash_bucket_holds_any_dns_name() -> None:
    template = (WEB / "nginx.conf.template").read_text(encoding="utf-8")
    sizes = re.findall(r"^server_names_hash_bucket_size (\d+);$", template, flags=re.MULTILINE)
    assert len(sizes) == 1
    # A bucket holds the name with a pointer and its length beside it.
    assert int(sizes[0]) >= LONGEST_DNS_NAME + 16
    assert len(LIMIT_HOST) == LONGEST_DNS_NAME


def _render(tmp_path: Path, content_url: str) -> Path:
    templates = tmp_path / "templates"
    conf = tmp_path / "conf.d"
    templates.mkdir()
    conf.mkdir()
    shutil.copy(WEB / "nginx.conf.template", templates / "default.conf.template")
    shutil.copy(WEB / "nginx.gateway.template", templates / "gateway.location.template")
    shutil.copy(WEB / "nginx.content.template", templates / "content.server.template")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "ALKERA_BACKEND_URL": "http://127.0.0.1:8000",
        "ALKERA_NGINX_TEMPLATES": str(templates),
        "ALKERA_NGINX_CONF_DIR": str(conf),
        "FILES_CONTENT_BASE_URL": content_url,
    }
    subprocess.run(["sh", str(WEB / "docker-entrypoint.sh")], env=env, check=True, timeout=30)
    return conf


@needs_envsubst
@pytest.mark.skipif(shutil.which("nginx") is None, reason="needs nginx")
@pytest.mark.parametrize(
    "host",
    [
        pytest.param(LONG_HOST, id="a-tunnel-name"),
        pytest.param(LIMIT_HOST, id="the-longest-dns-name"),
    ],
)
def test_nginx_starts_with_a_long_content_hostname(tmp_path: Path, host: str) -> None:
    conf = _render(tmp_path, f"https://{host}")
    assert (conf / "files-content.conf").read_text(encoding="utf-8").count(host) == 1
    main = tmp_path / "nginx.conf"
    main.write_text(
        f"pid {tmp_path / 'nginx.pid'};\nerror_log {tmp_path / 'error.log'};\n"
        f"events {{}}\nhttp {{\n  include {conf}/*.conf;\n}}\n",
        encoding="utf-8",
    )

    done = subprocess.run(
        ["nginx", "-t", "-p", str(tmp_path), "-c", str(main)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert done.returncode == 0, done.stderr
