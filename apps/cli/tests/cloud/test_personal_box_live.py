"""A person's own box registers against the real backend, on a real socket.

The CLI runs the device flow as ``alkera-box`` against the FastAPI app served
by uvicorn, the person's approval lands (written straight to the device row
over the sync DSN: the consent page is the backend suite's to drive), and the
box claims its machine on the credential it got back. What the wire proves and
a mock cannot: the server really issues a machine credential for this client,
the claim on it really succeeds, the credential then really stands as the box
(its heartbeat is accepted), and nothing the box keeps is a login.
"""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alkera_cli.account import auth_file
from alkera_cli.account.device_flow import DeviceCodeResponse
from alkera_cli.cloud.personal_box import load_personal_box, register_personal_box
from alkera_cli.host import paths
from alkera_core.auth import decode_session_token
from alkera_core.auth.token_hash import lookup_token_digests
from alkera_core.config import settings
from files._live_backend import live_backend
from sqlalchemy import create_engine, text


def _approve(device_code: str, user_id: str) -> None:
    sync = create_engine(settings.database_url_sync)
    try:
        with sync.begin() as connection:
            changed = connection.execute(
                text(
                    "UPDATE device_authorizations SET status = 'approved', user_id = :user, "
                    "approved_at = :now WHERE device_code_hash = ANY(:digests)"
                ),
                {
                    "user": user_id,
                    "now": datetime.now(UTC),
                    "digests": list(lookup_token_digests(device_code)),
                },
            )
            assert changed.rowcount == 1
    finally:
        sync.dispose()


def test_a_box_registers_through_the_device_flow_and_stands_on_its_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", home / "auth.yml")
    with live_backend(tmp_path / "server") as backend:
        owner = str(decode_session_token(backend.token).user_id)

        def approve(code: DeviceCodeResponse) -> None:
            _approve(code.device_code, owner)

        record = register_personal_box(
            backend.base_url,
            name="my laptop",
            daemon_version="0.5.0",
            announce=approve,
            sleep=lambda _s: None,
        )
        assert record.credential.startswith("alk_machine_")
        assert record.machine_id
        beat = httpx.post(
            f"{backend.base_url}/api/v1/machines/{record.machine_id}/heartbeat",
            headers={"Authorization": f"Bearer {record.credential}"},
            timeout=15,
        )
        assert beat.status_code == 204, beat.text

    stored = home / "personal_box.json"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert load_personal_box() == record
    assert not (home / "auth.yml").exists()
