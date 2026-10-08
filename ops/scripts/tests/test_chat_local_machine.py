"""ops/scripts/dev/chat-local-machine.sh, driven with a stand-in curl.

The script signs in, makes sure the org can run a machine (a compute grant),
then mints the machine's credential through the device flow. Each case runs it
with `--mint-only` against a fake `curl` that answers by URL and records every
call, so what is asserted is the requests the script made.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "dev" / "chat-local-machine.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash"
)

#: A curl that answers the stack's endpoints from $FAKE_DIR and logs each call.
FAKE_CURL = r"""#!/usr/bin/env bash
method=GET url=""
for arg in "$@"; do
  case "$arg" in
    PUT) method=PUT ;;
    http://*) url="$arg" ;;
  esac
done
printf '%s %s\n' "$method" "$url" >>"$FAKE_DIR/calls.log"
case "$method $url" in
  *"/health/live") echo '{"status": "ok"}' ;;
  *"/api/v1/auth/login") echo '{}' ;;
  "GET "*/api/v1/auth/me) echo '{"org_team_id": "org-1"}' ;;
  "GET "*/admin/v1/orgs/org-1/compute)
    [ -f "$FAKE_DIR/grants.json" ] || exit 22
    cat "$FAKE_DIR/grants.json" ;;
  "PUT "*/admin/v1/orgs/org-1/compute) echo '{}' ;;
  *"/api/v1/auth/device/code") echo '{"device_code": "dc", "user_code": "UC"}' ;;
  *"/api/v1/auth/device/approve") echo '{}' ;;
  *"/api/v1/auth/device/token") echo '{"access_token": "tok", "expires_in": 3600}' ;;
  *) echo "unexpected $method $url" >&2; exit 22 ;;
esac
"""


def _run(tmp_path: Path, grants: str | None) -> list[str]:
    fake = tmp_path / "fake"
    fake.mkdir()
    curl = fake / "curl"
    curl.write_text(FAKE_CURL, encoding="utf-8")
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    if grants is not None:
        (fake / "grants.json").write_text(grants, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(fake),
        "ALKERA_API_URL": "http://api.test",
        "CHAT_MACHINE_WEB_ORIGIN": "http://web.test",
        "CHAT_MACHINE_STATE_DIR": str(tmp_path / "state"),
        "CHAT_MACHINE_PYTHON": sys.executable,
    }
    done = subprocess.run(
        ["bash", str(SCRIPT), "--mint-only"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    auth = (tmp_path / "state" / "home" / "auth.yml").read_text(encoding="utf-8")
    assert "tok" in auth
    return (fake / "calls.log").read_text(encoding="utf-8").splitlines()


def test_an_org_without_a_grant_is_granted_compute_before_the_device_flow(
    tmp_path: Path,
) -> None:
    calls = _run(tmp_path, grants="[]")
    assert calls.index("PUT http://api.test/admin/v1/orgs/org-1/compute") < calls.index(
        "GET http://api.test/api/v1/auth/device/code"
    )


@pytest.mark.parametrize(
    "grants",
    [
        pytest.param('[{"id": "g-1", "ceiling": 1}]', id="org-already-holds-a-grant"),
        pytest.param(None, id="user-is-not-a-platform-admin"),
    ],
)
def test_no_grant_is_written_unless_the_org_lacks_one_and_the_user_may_grant(
    tmp_path: Path, grants: str | None
) -> None:
    calls = _run(tmp_path, grants=grants)
    assert not any(call.startswith("PUT ") for call in calls)
    assert "GET http://api.test/api/v1/auth/device/token" in calls
