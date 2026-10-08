"""Log this machine's Alkera clients into a local dev stack, non-interactively.

The dev counterpart of `alkera login`: signs in as the seeded dev admin
(created by `make seed`), self-approves an RFC 8628 device code against the
given workspace backend, and writes `~/.alkera/auth.yml` through the CLI's own
helpers. Any running daemon notices the file within seconds (auth watcher) and
flips to the new session.

Refuses a non-localhost API, since the seeded admin only exists in a local dev
database. An auth.yml minted for another host (for example prod) is backed up
to `auth.yml.bak` before being replaced.

Usage: .venv/bin/python scripts/dev-login.py http://localhost:37120
"""

from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime, timedelta

import httpx
from alkera_cli.account import auth_file, device_flow
from alkera_cli.account.auth_file import StoredAuth
from alkera_cli.account.jwt_decode import jwt_expires_at
from alkera_cli.host.paths import AUTH_FILE_PATH
from alkera_core.config import settings

DEV_EMAIL = settings.auth_dev_admin_email
DEV_PASSWORD = settings.auth_dev_admin_password
# Keep a stored token that still has at least this much life left.
MIN_REMAINING = timedelta(hours=1)


def _still_good(stored: StoredAuth | None, api: str) -> bool:
    if stored is None or stored.api_url.rstrip("/") != api:
        return False
    expires = stored.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires > datetime.now(UTC) + MIN_REMAINING


def _mint_token(api: str) -> str:
    """Cookie-login as the seeded admin, then approve our own device code."""
    with httpx.Client(base_url=api, timeout=10.0) as client:
        client.post(
            "/api/v1/auth/login", json={"email": DEV_EMAIL, "password": DEV_PASSWORD}
        ).raise_for_status()
        device = device_flow.request_device_code(api, client_id=device_flow.CLIENT_ID_CLI)
        client.post(
            "/api/v1/auth/device/approve", json={"user_code": device.user_code}
        ).raise_for_status()
    return device_flow.poll_for_token(
        api,
        device.device_code,
        client_id=device_flow.CLIENT_ID_CLI,
        interval=device.interval,
        expires_in=device.expires_in,
    )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: dev-login.py <api-url>", file=sys.stderr)
        return 2
    api = sys.argv[1].rstrip("/")
    if httpx.URL(api).host not in ("localhost", "127.0.0.1"):
        print(f"refusing dev login against non-local API {api}", file=sys.stderr)
        return 1

    stored = auth_file.load_auth()
    if _still_good(stored, api):
        print(f"auth.yml already targets {api}; keeping it")
        return 0

    try:
        token = _mint_token(api)
    except (httpx.HTTPError, device_flow.DeviceFlowError) as exc:
        print(f"dev login against {api} failed: {exc}", file=sys.stderr)
        return 1

    if stored is not None and stored.api_url.rstrip("/") != api:
        backup = AUTH_FILE_PATH.parent / (AUTH_FILE_PATH.name + ".bak")
        shutil.copy2(AUTH_FILE_PATH, backup)
        print(f"backed up {stored.api_url} credentials to {backup}")
    auth_file.save_auth(StoredAuth(api_url=api, token=token, expires_at=jwt_expires_at(token)))
    print(f"logged in as {DEV_EMAIL} against {api}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
