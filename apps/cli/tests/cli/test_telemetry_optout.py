"""The unified telemetry opt-out gates Sentry init for the CLI and the daemon.

`telemetry.reconcile_sentry` is the SINGLE gate both surfaces use — the
CLI crash path (the product's crash-report hook, tested with it) and the daemon
(`daemon.server.run_stdio` calls `reconcile_sentry("daemon")`, and so does the
daemon's `preferences.set` handler so a toggle re-gates it live). These tests
pin that gate directly (including the precedence between the three opt-outs).
The daemon wires the same helper at startup; `run_stdio` owns the stdio server
loop and isn't driven here, so its coverage is the shared-gate tests (exercised
with the "daemon" component) plus that one wiring line.

Sentry's real `init_sentry` (and `capture_exception`) are mocked — they are the
costly/global boundary. The assertions are about WHETHER init is called given the
opt-out, never about Sentry internals. These fail on the pre-fix code (which
called `init_sentry` unconditionally) and pass with the gate in place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli.host import paths
from alkera_cli.observability import telemetry


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``~/.alkera`` (and the preferences file) at a tmp dir, so a test's
    preferences never touch the developer's real file."""
    h = tmp_path / "alkera-home"
    h.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", h)
    monkeypatch.setattr(paths, "PREFERENCES_FILE_PATH", h / "preferences.yml")
    return h


@pytest.fixture
def telemetry_clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient env opt-out — so a test isolates the preference/arg under test
    (CI is set in the real CI runner and would otherwise force telemetry off)."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ALKERA_TELEMETRY", raising=False)


@pytest.fixture
def fake_init(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Record calls to Sentry's `init_sentry` (the boundary). The helper imports
    it function-locally, so patching the source module attribute intercepts it."""
    calls: list[tuple[str, object]] = []

    def _fake(component: str, *, dsn: object = None) -> bool:
        calls.append((component, dsn))
        return True

    monkeypatch.setattr("alkera_core.observability.sentry.init_sentry", _fake)
    return calls


@pytest.fixture
def fake_shutdown(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Record calls to Sentry's `shutdown_sentry` (the disable boundary)."""
    calls: list[None] = []
    monkeypatch.setattr(
        "alkera_core.observability.sentry.shutdown_sentry", lambda: calls.append(None)
    )
    return calls


# --- the shared gate (used by BOTH the CLI crash path and the daemon) ---------


def test_init_when_enabled_by_default(
    home: Path, telemetry_clean_env: None, fake_init: list[tuple[str, object]]
) -> None:
    """No opt-out anywhere → Sentry is initialized for the component."""
    assert telemetry.reconcile_sentry("daemon") is True
    assert [c for c, _ in fake_init] == ["daemon"]


@pytest.mark.parametrize("token", ["0", "false", "FALSE", "no", "off"])
def test_skips_on_env_opt_out(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_init: list[tuple[str, object]],
    token: str,
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("ALKERA_TELEMETRY", token)
    assert telemetry.reconcile_sentry("cli") is False
    assert fake_init == []


def test_skips_under_ci(
    home: Path, monkeypatch: pytest.MonkeyPatch, fake_init: list[tuple[str, object]]
) -> None:
    monkeypatch.delenv("ALKERA_TELEMETRY", raising=False)
    monkeypatch.setenv("CI", "true")
    assert telemetry.reconcile_sentry("daemon") is False
    assert fake_init == []


def test_skips_when_preference_disabled(
    home: Path, telemetry_clean_env: None, fake_init: list[tuple[str, object]]
) -> None:
    """The `telemetry_enabled: false` preference (what the TUI Privacy page + the
    VS Code preferences page write) disables Sentry init."""
    (home / "preferences.yml").write_text("telemetry_enabled: false\n", encoding="utf-8")
    assert telemetry.reconcile_sentry("cli") is False
    assert fake_init == []


def test_inits_when_preference_enabled(
    home: Path, telemetry_clean_env: None, fake_init: list[tuple[str, object]]
) -> None:
    """The explicit `telemetry_enabled: true` preference keeps Sentry on."""
    (home / "preferences.yml").write_text("telemetry_enabled: true\n", encoding="utf-8")
    assert telemetry.reconcile_sentry("cli") is True
    assert [c for c, _ in fake_init] == ["cli"]


# --- live toggle: reconcile flips between enable (init) and disable (shutdown) -


def test_reconcile_toggles_between_init_and_shutdown(
    home: Path,
    telemetry_clean_env: None,
    fake_init: list[tuple[str, object]],
    fake_shutdown: list[None],
) -> None:
    """The whole point of `reconcile_sentry`: called repeatedly as the preference
    flips, it enables (init) then disables (shutdown) then re-enables — so a
    long-running daemon stops/starts reporting live, not only on restart."""
    # On by default → init, no shutdown.
    assert telemetry.reconcile_sentry("daemon") is True
    assert [c for c, _ in fake_init] == ["daemon"]
    assert fake_shutdown == []

    # Toggle off → shutdown, no further init.
    (home / "preferences.yml").write_text("telemetry_enabled: false\n", encoding="utf-8")
    assert telemetry.reconcile_sentry("daemon") is False
    assert len(fake_shutdown) == 1
    assert [c for c, _ in fake_init] == ["daemon"]

    # Toggle back on → init again (re-enabled live).
    (home / "preferences.yml").write_text("telemetry_enabled: true\n", encoding="utf-8")
    assert telemetry.reconcile_sentry("daemon") is True
    assert [c for c, _ in fake_init] == ["daemon", "daemon"]
    assert len(fake_shutdown) == 1


# --- precedence: the three opt-outs are independent, "any one off wins" -------


def test_env_off_overrides_preference_on(
    home: Path, monkeypatch: pytest.MonkeyPatch, fake_init: list[tuple[str, object]]
) -> None:
    """`ALKERA_TELEMETRY=0` disables even when the preference is explicitly on —
    a truthy preference cannot re-enable telemetry the env var turned off."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("ALKERA_TELEMETRY", "0")
    (home / "preferences.yml").write_text("telemetry_enabled: true\n", encoding="utf-8")
    assert telemetry.reconcile_sentry("cli") is False
    assert fake_init == []


def test_preference_off_overrides_env_on(
    home: Path, monkeypatch: pytest.MonkeyPatch, fake_init: list[tuple[str, object]]
) -> None:
    """A truthy `ALKERA_TELEMETRY` does NOT re-enable telemetry when the
    preference is off — env-on cannot override a preference opt-out."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("ALKERA_TELEMETRY", "1")
    (home / "preferences.yml").write_text("telemetry_enabled: false\n", encoding="utf-8")
    assert telemetry.reconcile_sentry("cli") is False
    assert fake_init == []


# --- the CLI crash path honors the gate end-to-end ---------------------------
