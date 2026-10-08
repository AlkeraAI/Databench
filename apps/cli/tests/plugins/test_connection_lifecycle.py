"""Connection lifecycle beyond add/remove — the enterprise edit/test/mute/clone/
rename operations on the registry.

A purpose-built ``LifecyclePlugin`` gives a real RunSQL probe (so validate actually
runs) and a form whose BLANK secret field returns no ``Credential`` (the "keep the
current secret" edit path). The module-level ``_PROBE`` flag flips the probe from
pass to fail so the rollback invariants are exercised end-to-end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alkera_cli.plugins.plugin_base import (
    ActivationSpec,
    CapabilitySet,
    Connection,
    ConnectionFormSchema,
    ConnectionValidationError,
    Credential,
    CredentialMode,
    Environment,
    FormField,
    LocalCredentialManager,
    Plugin,
    PluginManifest,
    PluginRegistry,
    Registrar,
    RunSQLCapability,
    SurfaceKind,
    TokenCredential,
)
from alkera_cli.plugins.plugin_base.connection_form import AuthMethodSchema
from alkera_core.project.directory import ProjectDirectory
from pydantic import SecretStr

_PROBE = {"ok": True}


class _ProbeRunSQL(RunSQLCapability):
    async def run(self, sql, *, effect, cap_token, limit):  # type: ignore[no-untyped-def]
        if not _PROBE["ok"]:
            raise RuntimeError("FATAL: password authentication failed")
        from alkera_cli.plugins.plugin_base import QueryResult

        return QueryResult(columns=["one"], rows=[[1]], row_count=1)


class _ProbeCaps:
    def capabilities(self, conn: Connection) -> CapabilitySet:
        return CapabilitySet({RunSQLCapability: _ProbeRunSQL()})


class LifecyclePlugin(Plugin):
    manifest = PluginManifest(
        name="lc", surfaces=frozenset({SurfaceKind.CONNECTION, SurfaceKind.CAPABILITY})
    )

    def register(self, r: Registrar) -> None:
        r.capabilities(_ProbeCaps)

    def activation(self) -> ActivationSpec:
        return ActivationSpec(workspace_contains=["never_matches.marker"])

    def connection_form_schema(self) -> ConnectionFormSchema:
        return ConnectionFormSchema(
            auth_methods=[
                AuthMethodSchema(
                    name="pat",
                    label="Token",
                    fields=[FormField(name="token", label="Token", secret=True, required=False)],
                )
            ],
            shared_fields=[FormField(name="host", label="Host", required=True)],
        )

    def build_connection(
        self, handle: str, auth_method: str, fields: dict[str, str]
    ) -> tuple[Connection, Credential | None]:
        conn = Connection(
            handle=handle,
            plugin="lc",
            environment=Environment.PROD,
            attributes={"host": fields.get("host", "")},
        )
        token = fields.get("token") or ""
        # A blank secret field ⇒ no new credential (the edit KEEPS the current secret).
        cred = TokenCredential(token=SecretStr(token)) if token else None
        return conn, cred


@pytest.fixture(autouse=True)
def _reset_probe() -> None:
    _PROBE["ok"] = True


async def _registry(tmp_path: Path) -> PluginRegistry:
    registry = PluginRegistry(ProjectDirectory(tmp_path / ".alkera"), tmp_path)
    await registry.discover(extra_plugins=[LifecyclePlugin])
    return registry


def _cred_file(tmp_path: Path, handle: str) -> Path:
    return tmp_path / ".alkera" / "plugins" / "lc" / "connections" / handle / "credential"


async def _add(
    registry: PluginRegistry, handle: str, token: str = "tok-1", host: str = "h1"
) -> Connection:
    return await registry.configure_connection(
        "lc",
        handle,
        "pat",
        {"token": token, "host": host},
        credential_manager=LocalCredentialManager(),
    )


# --- mute / unmute -----------------------------------------------------------


async def test_set_enabled_toggles_and_filters_from_the_tool_surface(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "wh")
    # Live + enabled ⇒ the connection is resolvable by the agent's tools.
    assert registry.tool_registry().connection_for("wh") is not None

    muted = await registry.set_connection_enabled("lc", "wh", False)
    assert muted is not None and muted.enabled is False
    # The connection is still in the store (config + secret kept) …
    assert any(c.handle == "wh" for c in registry.added_connections())
    assert _cred_file(tmp_path, "wh").exists()
    # … but it drops off the agent's surface.
    assert registry.tool_registry().connection_for("wh") is None

    again = await registry.set_connection_enabled("lc", "wh", True)
    assert again is not None and again.enabled is True
    assert registry.tool_registry().connection_for("wh") is not None


async def test_set_enabled_unknown_handle_returns_none(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    assert await registry.set_connection_enabled("lc", "nope", False) is None


# --- test (re-probe) ---------------------------------------------------------


async def test_test_connection_succeeds_when_reachable(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "wh")
    await registry.test_connection("lc", "wh")  # no raise == healthy


async def test_test_connection_raises_the_real_error_when_broken(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "wh")
    _PROBE["ok"] = False
    with pytest.raises(ConnectionValidationError, match="authentication failed"):
        await registry.test_connection("lc", "wh")
    # A failed health check does NOT remove the connection.
    assert any(c.handle == "wh" for c in registry.added_connections())


async def test_test_connection_unknown_handle_raises_valueerror(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    with pytest.raises(ValueError, match="no added connection"):
        await registry.test_connection("lc", "ghost")


# --- edit --------------------------------------------------------------------


async def test_update_non_secret_field_keeps_the_existing_secret(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "wh", token="keep-me", host="old")
    before = _cred_file(tmp_path, "wh").read_bytes()

    updated = await registry.update_connection(
        "lc", "wh", "pat", {"token": "", "host": "new"}, credential_manager=LocalCredentialManager()
    )
    assert updated.attributes["host"] == "new"
    # The blank token field left the secret untouched on disk.
    assert _cred_file(tmp_path, "wh").read_bytes() == before
    assert registry.tool_registry().connection_for("wh") is not None


async def test_update_changing_the_secret_overwrites_it(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "wh", token="old-secret")
    manager = LocalCredentialManager()

    await registry.update_connection(
        "lc", "wh", "pat", {"token": "new-secret", "host": "h1"}, credential_manager=manager
    )
    conn = next(c for c in registry.added_connections() if c.handle == "wh")
    assert conn.credential_ref is not None
    cred = manager.resolve_sync(conn.credential_ref)
    assert isinstance(cred, TokenCredential)
    assert cred.token.get_secret_value() == "new-secret"


async def test_update_rolls_back_the_secret_when_the_probe_fails(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "wh", token="good-secret")
    original = _cred_file(tmp_path, "wh").read_bytes()

    _PROBE["ok"] = False  # the edited connection can't connect
    with pytest.raises(ConnectionValidationError):
        await registry.update_connection(
            "lc",
            "wh",
            "pat",
            {"token": "bad-new-secret", "host": "h1"},
            credential_manager=LocalCredentialManager(),
        )
    # The old secret is restored byte-for-byte — a rejected edit changes nothing on disk.
    assert _cred_file(tmp_path, "wh").read_bytes() == original


async def test_update_preserves_enabled_and_mode(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "wh")
    await registry.set_connection_enabled("lc", "wh", False)
    # stamp a per-user mode to prove it carries through an edit
    muted = next(c for c in registry.added_connections() if c.handle == "wh")
    registry._added.add(muted.model_copy(update={"credential_mode": CredentialMode.PER_USER}))

    updated = await registry.update_connection(
        "lc", "wh", "pat", {"token": "", "host": "h2"}, credential_manager=LocalCredentialManager()
    )
    assert updated.enabled is False
    assert updated.credential_mode is CredentialMode.PER_USER


async def test_update_unknown_connection_raises(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    with pytest.raises(ValueError, match="no added connection"):
        await registry.update_connection(
            "lc",
            "ghost",
            "pat",
            {"token": "t", "host": "h"},
            credential_manager=LocalCredentialManager(),
        )


# --- clone -------------------------------------------------------------------


async def test_clone_copies_config_without_the_secret_and_starts_muted(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "prod", host="prod.example.com")

    clone = await registry.clone_connection("lc", "prod", "staging")
    assert clone.handle == "staging"
    assert clone.attributes["host"] == "prod.example.com"  # config copied
    assert clone.credential_ref is None  # secret NOT copied
    assert clone.enabled is False  # muted until the user gives it a secret
    # No credential file exists for the clone yet.
    assert not _cred_file(tmp_path, "staging").exists()


async def test_clone_rejects_a_duplicate_handle(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "a")
    await _add(registry, "b")
    with pytest.raises(ValueError, match="already exists"):
        await registry.clone_connection("lc", "a", "b")


# --- rename ------------------------------------------------------------------


async def test_rename_moves_the_credential_dir_and_repoints_the_ref(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "old_name", token="the-secret")
    old_file = _cred_file(tmp_path, "old_name")
    assert old_file.exists()

    renamed = await registry.rename_connection("lc", "old_name", "new_name")
    assert renamed.handle == "new_name"
    # The credential moved with it and the ref points at the new path.
    assert not old_file.exists()
    new_file = _cred_file(tmp_path, "new_name")
    assert new_file.exists()
    assert renamed.credential_ref is not None
    assert renamed.credential_ref.locator == str(new_file)
    # The secret is intact + resolvable at the new location.
    cred = LocalCredentialManager().resolve_sync(renamed.credential_ref)
    assert isinstance(cred, TokenCredential) and cred.token.get_secret_value() == "the-secret"

    handles = {c.handle for c in registry.added_connections()}
    assert handles == {"new_name"}  # exactly one entry, under the new handle


async def test_rename_to_existing_handle_is_rejected(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "a")
    await _add(registry, "b")
    with pytest.raises(ValueError, match="already exists"):
        await registry.rename_connection("lc", "a", "b")
    # Both survive the rejected rename.
    assert {c.handle for c in registry.added_connections()} == {"a", "b"}


async def test_rename_unknown_source_raises(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    with pytest.raises(ValueError, match="no added connection"):
        await registry.rename_connection("lc", "ghost", "x")


@pytest.mark.parametrize("bad", ["../escape", "has space", "a/b", "."])
async def test_lifecycle_rejects_unsafe_new_handles(tmp_path: Path, bad: str) -> None:
    registry = await _registry(tmp_path)
    await _add(registry, "src")
    with pytest.raises(ValueError):
        await registry.rename_connection("lc", "src", bad)
    with pytest.raises(ValueError):
        await registry.clone_connection("lc", "src", bad)


def _painted(_: Any) -> str:  # pragma: no cover - reserved for future UI-level assertions
    return ""
