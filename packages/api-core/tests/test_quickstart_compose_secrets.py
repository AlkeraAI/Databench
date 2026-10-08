"""The one-machine ``compose.yaml`` never signs a session with a published secret.

It runs ``APP_ENV=local``, where an unset ``AUTH_JWT_SECRET`` falls back to a
value published in this repository: anything that reaches the API could mint a
session for the seeded platform admin, and the secrets-at-rest key derived from
it would be public too. Every Python service therefore reads its server secrets
from ``GENERATED_SECRETS_DIR`` on a volume they all mount.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from _settings_env import seal_settings_env
from alkera_core.config import _PUBLISHED_DEV_SECRETS, Settings

#: The services that boot ``alkera_core.config.Settings``.
PYTHON_SERVICES = ("init", "backend", "worker", "gateway")


def _compose() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    for candidate in (root / "compose.yaml", root / "Databench" / "compose.yaml"):
        if candidate.is_file():
            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            assert isinstance(loaded, dict)
            return loaded
    raise AssertionError(f"no compose.yaml under {root}")


@pytest.mark.parametrize("service", PYTHON_SERVICES)
def test_every_python_service_reads_generated_secrets_from_a_shared_volume(
    service: str,
) -> None:
    spec = _compose()["services"][service]
    directory = spec["environment"].get("GENERATED_SECRETS_DIR")
    assert directory, f"{service} has no GENERATED_SECRETS_DIR"
    mounts = [str(v) for v in spec.get("volumes", [])]
    assert any(m.endswith(f":{directory}") and not m.startswith(("/", ".")) for m in mounts), (
        f"{service} does not mount a named volume at {directory}: {mounts}"
    )
    for name in ("AUTH_JWT_SECRET", "TOKEN_HASH_PEPPER", "FILES_CONTENT_SIGNING_KEY"):
        assert not spec["environment"].get(name), f"{service} pins {name}"


def test_the_volume_is_handed_to_the_app_user_before_any_service_writes() -> None:
    services = _compose()["services"]

    def upstream(name: str) -> set[str]:
        found: set[str] = set()
        pending = [name]
        while pending:
            for dep in services[pending.pop()].get("depends_on", {}):
                if dep not in found:
                    found.add(dep)
                    pending.append(dep)
        return found

    for service in PYTHON_SERVICES:
        assert "secrets-init" in upstream(service), f"{service} can start before secrets-init"
    assert "chown 1000:1000" in " ".join(services["secrets-init"]["command"])


def test_settings_built_from_the_compose_environment_carry_no_published_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seal_settings_env(monkeypatch)
    env = _compose()["services"]["backend"]["environment"]
    assert env["APP_ENV"] == "local"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, app_env="local", generated_secrets_dir=str(tmp_path)
    )
    signing = settings.files_content_signing_key
    values = {
        settings.effective_jwt_secret,
        settings.token_hash_pepper or "",
        signing.get_secret_value() if signing is not None else "",
    }
    assert values.isdisjoint(_PUBLISHED_DEV_SECRETS)
    assert all(len(v) >= 64 for v in values)
    assert settings.secret_box_key
