"""Layered CLI config resolution: env > system file > defaults.

These tests exercise `get_settings()` by toggling the `ALKERA_SYSTEM_CONFIG`
override + env vars, then re-importing `config` so the `lru_cache` and
pydantic-settings caches don't carry over between cases.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

#: The checkout these tests live in: this file is apps/cli/tests/cli/<this>.py.
TESTS_CHECKOUT = Path(__file__).resolve().parents[4]


def _cli_imported_from_this_checkout() -> bool:
    """Whether ``alkera_cli`` is this checkout's source, decided without the
    lookup under test. Only an installed CLI run against these tests (a wheel
    in site-packages) is not."""
    from alkera_cli.host import config

    return Path(config.__file__).resolve().is_relative_to(TESTS_CHECKOUT / "apps" / "cli")


@pytest.fixture
def fresh_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Return a callable that yields a freshly-imported `get_settings()`.

    pydantic-settings caches env reads at first import; `get_settings()`
    caches via `lru_cache`. Re-importing the module under monkeypatched env
    is the cleanest way to verify each layer.
    """
    # Don't leak the developer's actual env into the test.
    monkeypatch.delenv("ALKERA_API_URL", raising=False)
    monkeypatch.delenv("ALKERA_FRONTEND_URL", raising=False)
    monkeypatch.delenv("ALKERA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("ALKERA_SYSTEM_CONFIG", raising=False)
    # Run from an empty dir so pydantic-settings' relative dotenv reads (`.env`,
    # `.env.workspace`) resolve to nothing — these tests assert the layered
    # resolution in a vacuum, independent of the dev's repo-root env files.
    monkeypatch.chdir(tmp_path)
    # Force the system path to a tmp file so /etc/alkera/config.yml on the
    # CI host (if any) doesn't influence the test.
    monkeypatch.setenv("ALKERA_SYSTEM_CONFIG", str(tmp_path / "system.yml"))

    def reload() -> object:
        from alkera_cli.host import config, paths

        importlib.reload(paths)
        importlib.reload(config)
        return config.get_settings()

    yield reload

    # Re-reload under the RESTORED env/cwd so `alkera_cli.host.config.get_settings`
    # doesn't stay bound to a cache built from this test's monkeypatched world —
    # without this, every later `from alkera_cli.host.config import get_settings`
    # resolves to corp/tmp values from whichever case ran last.
    from alkera_cli.host import config, paths

    importlib.reload(paths)
    importlib.reload(config)


def test_defaults_when_no_env_no_system(fresh_config):
    settings = fresh_config()
    assert settings.alkera_api_url == "http://localhost:8000"
    assert settings.alkera_frontend_url == "http://localhost:5173"


def test_system_file_overrides_defaults(
    fresh_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sys_file = tmp_path / "system.yml"
    sys_file.write_text(
        "api_url: https://api.corp.internal\nfrontend_url: https://app.corp.internal\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALKERA_SYSTEM_CONFIG", str(sys_file))
    settings = fresh_config()
    assert settings.alkera_api_url == "https://api.corp.internal"
    assert settings.alkera_frontend_url == "https://app.corp.internal"


def test_env_var_wins_over_system_file(
    fresh_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sys_file = tmp_path / "system.yml"
    sys_file.write_text("api_url: https://api.corp.internal\n", encoding="utf-8")
    monkeypatch.setenv("ALKERA_SYSTEM_CONFIG", str(sys_file))
    monkeypatch.setenv("ALKERA_API_URL", "https://override-from-env.example")
    settings = fresh_config()
    assert settings.alkera_api_url == "https://override-from-env.example"


def test_malformed_system_file_falls_back_silently(
    fresh_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sys_file = tmp_path / "system.yml"
    sys_file.write_text("not: valid: yaml: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("ALKERA_SYSTEM_CONFIG", str(sys_file))
    settings = fresh_config()
    # Falls back to defaults rather than crashing.
    assert settings.alkera_api_url == "http://localhost:8000"


def test_system_file_with_partial_keys_only_overrides_what_it_has(
    fresh_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sys_file = tmp_path / "system.yml"
    sys_file.write_text("api_url: https://api.corp.internal\n", encoding="utf-8")
    monkeypatch.setenv("ALKERA_SYSTEM_CONFIG", str(sys_file))
    settings = fresh_config()
    assert settings.alkera_api_url == "https://api.corp.internal"
    # frontend_url falls through to default
    assert settings.alkera_frontend_url == "http://localhost:5173"


def test_missing_system_file_is_silent(fresh_config, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALKERA_SYSTEM_CONFIG", "/nonexistent/alkera-system.yml")
    settings = fresh_config()
    assert settings.alkera_api_url == "http://localhost:8000"


def test_gateway_url_default(fresh_config):
    assert fresh_config().alkera_gateway_url == "http://localhost:8081"


def test_gateway_url_from_system_file(
    fresh_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sys_file = tmp_path / "system.yml"
    sys_file.write_text("gateway_url: https://gw.corp.internal\n", encoding="utf-8")
    monkeypatch.setenv("ALKERA_SYSTEM_CONFIG", str(sys_file))
    assert fresh_config().alkera_gateway_url == "https://gw.corp.internal"


def test_gateway_url_env_wins_over_system_file(
    fresh_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sys_file = tmp_path / "system.yml"
    sys_file.write_text("gateway_url: https://gw.corp.internal\n", encoding="utf-8")
    monkeypatch.setenv("ALKERA_SYSTEM_CONFIG", str(sys_file))
    monkeypatch.setenv("ALKERA_GATEWAY_URL", "https://gw.env.example")
    assert fresh_config().alkera_gateway_url == "https://gw.env.example"


def test_ci_safe_api_url_ignores_the_checkout_dotenv(
    fresh_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A CI token is a bearer credential, so its destination must NOT come from a
    PR-controlled ``.env`` in the checkout — only the process env, the system
    config, and the built-in default."""
    (tmp_path / ".env").write_text("ALKERA_API_URL=https://attacker.example\n", encoding="utf-8")
    fresh_config()  # reloads config with cwd == tmp_path
    from alkera_cli.host import config

    # ci_safe_api_url ignores the dotenv -> the safe built-in default.
    assert config.ci_safe_api_url() == config.DEFAULT_API_URL
    # A PROCESS env var (what the official CI action exports) IS honored.
    monkeypatch.setenv("ALKERA_API_URL", "https://api.trusted.example")
    assert config.ci_safe_api_url() == "https://api.trusted.example"


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("ALKERA_API_URL", id="api"),
        pytest.param("ALKERA_GATEWAY_URL", id="gateway"),
        pytest.param("ALKERA_FRONTEND_URL", id="frontend"),
    ],
)
def test_a_dotenv_in_the_working_directory_cannot_redirect_an_endpoint(
    fresh_config, tmp_path: Path, key: str
):
    """The CLI runs inside whatever repository the user cloned, and the
    resolved API/gateway URL is where the user's bearer token is sent. A `.env`
    committed to that repository must therefore not be read at all — the endpoints
    stay on the trusted layers (process env > system config > built-in default)."""
    (tmp_path / ".env").write_text(f"{key}=https://attacker.example\n", encoding="utf-8")
    settings = fresh_config()  # reloads config with cwd == tmp_path
    from alkera_cli.host import config

    assert settings.alkera_api_url == config.DEFAULT_API_URL
    assert settings.alkera_gateway_url == config.DEFAULT_GATEWAY_URL
    assert settings.alkera_frontend_url == config.DEFAULT_FRONTEND_URL
    assert config._dotenv_files() == ()


def test_env_workspace_in_the_working_directory_cannot_redirect_an_endpoint(
    fresh_config, tmp_path: Path
):
    """Same for the second dotenv name — `.env.workspace` is generated per worktree,
    so a cloned repo shipping one must not be honored either."""
    (tmp_path / ".env.workspace").write_text(
        "ALKERA_GATEWAY_URL=https://gw.attacker.example\n", encoding="utf-8"
    )
    settings = fresh_config()
    from alkera_cli.host import config

    assert settings.alkera_gateway_url == config.DEFAULT_GATEWAY_URL


def test_a_repo_mimicking_the_source_layout_is_still_not_trusted(fresh_config, tmp_path: Path):
    """The asymmetric case: trust is anchored on the checkout the running code came
    FROM, not on the nearest directory above the CWD that looks like one. A hostile
    repo that ships empty `apps/cli/alkera_cli/` + `packages/api-core/alkera_core/`
    directories next to its `.env` gets nothing."""
    (tmp_path / "apps" / "cli" / "alkera_cli").mkdir(parents=True)
    (tmp_path / "packages" / "api-core" / "alkera_core").mkdir(parents=True)
    (tmp_path / ".env").write_text("ALKERA_API_URL=https://attacker.example\n", encoding="utf-8")
    settings = fresh_config()
    from alkera_cli.host import config

    assert config._looks_like_source_checkout(tmp_path)  # it does look like one…
    assert config._dotenv_files() == ()  # …and is still refused.
    assert settings.alkera_api_url == config.DEFAULT_API_URL


def test_no_dotenv_layer_at_all_when_not_running_from_a_source_checkout(
    monkeypatch: pytest.MonkeyPatch,
):
    """An installed wheel / compiled binary has no checkout to anchor on, so the
    dotenv layer is empty everywhere — including inside the real repo."""
    from alkera_cli.host import config

    monkeypatch.setattr(config, "_package_checkout_root", lambda: None)
    assert config._dotenv_files() == ()


def test_this_checkouts_dotenv_is_read_from_a_subdirectory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The preserved dev workflow: dev shells run `alkera` from a subdirectory
    such as `apps/cli`, and the generated `.env.workspace` with this worktree's
    ports lives two directories above. Asserts the resolved FILE LIST (not its
    values, which differ per worktree)."""
    from alkera_cli.host import config, paths

    if not _cli_imported_from_this_checkout():
        pytest.skip("alkera_cli is an installed distribution, not this checkout's source")
    # In a source checkout the lookup must find it: a skip here once hid a
    # restructure that broke it.
    root = config._package_checkout_root()
    assert root == TESTS_CHECKOUT
    nested = root / "apps" / "cli"
    monkeypatch.setenv("ALKERA_SYSTEM_CONFIG", str(tmp_path / "system.yml"))
    monkeypatch.chdir(nested)

    importlib.reload(paths)
    importlib.reload(config)
    try:
        assert config._dotenv_files() == (str(root / ".env"), str(root / ".env.workspace"))
    finally:
        # Restore the real cwd/env BEFORE the final reload so the module doesn't stay
        # bound to this test's world for whatever runs next in the session.
        monkeypatch.undo()
        importlib.reload(paths)
        importlib.reload(config)


# ---------------------------------------------------------------------------
# The dotenv layer is re-decided per settings LOAD, not frozen at import
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A stand-in source checkout that `_package_checkout_root()` trusts, holding a
    `.env` that redirects the API URL. Lets a test move the CWD in and out of the
    trusted root inside ONE process — no module reload, which is exactly what an
    import-time-frozen `env_file` would hide."""
    from alkera_cli.host import config

    checkout = tmp_path / "checkout"
    (checkout / "apps" / "cli" / "alkera_cli").mkdir(parents=True)
    (checkout / "packages" / "api-core" / "alkera_core").mkdir(parents=True)
    (checkout / ".env").write_text("ALKERA_API_URL=https://from-dotenv.example\n", encoding="utf-8")
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setattr(config, "_package_checkout_root", lambda: checkout)
    for var in ("ALKERA_API_URL", "ALKERA_FRONTEND_URL", "ALKERA_GATEWAY_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALKERA_SYSTEM_CONFIG", str(tmp_path / "absent.yml"))
    return checkout


def test_dotenv_layer_follows_the_cwd_within_one_process(
    fake_checkout: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The rule `_dotenv_files` documents — this checkout's dotenv counts only while
    the CWD is inside it — has to hold for the settings load happening NOW.

    Both directions in one process: a load from inside the checkout picks the file
    up (the preserved dev workflow), and a later load from outside it must not,
    which is impossible if the decision was frozen when the module was imported.
    """
    from alkera_cli.host import config

    monkeypatch.chdir(fake_checkout)
    assert config.CliSettings().alkera_api_url == "https://from-dotenv.example"

    monkeypatch.chdir(tmp_path / "elsewhere")
    assert config.CliSettings().alkera_api_url == config.DEFAULT_API_URL

    # …and back in again: the layer returns, it isn't latched off either.
    monkeypatch.chdir(fake_checkout / "apps" / "cli")
    assert config.CliSettings().alkera_api_url == "https://from-dotenv.example"


def test_get_settings_resolves_the_dotenv_layer_when_called_not_when_imported(
    fake_checkout: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The layered accessor sees the same live decision (it caches the first
    resolution, so each case clears it)."""
    from alkera_cli.host import config

    try:
        monkeypatch.chdir(tmp_path / "elsewhere")
        config.get_settings.cache_clear()
        assert config.get_settings().alkera_api_url == config.DEFAULT_API_URL

        monkeypatch.chdir(fake_checkout)
        config.get_settings.cache_clear()
        assert config.get_settings().alkera_api_url == "https://from-dotenv.example"
    finally:
        # Never leave a cached instance built from this test's world behind.
        config.get_settings.cache_clear()


def test_a_hostile_cwd_is_still_refused_after_the_per_load_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The asymmetric case the per-load evaluation must NOT weaken: re-deciding on
    every load still anchors trust on the checkout the running code came from, so a
    cloned repo the CLI is merely invoked inside gets no vote — even though its
    `.env` is now consulted at a moment when the CWD is that repo."""
    from alkera_cli.host import config

    hostile = tmp_path / "hostile-repo"
    (hostile / "apps" / "cli" / "alkera_cli").mkdir(parents=True)
    (hostile / "packages" / "api-core" / "alkera_core").mkdir(parents=True)
    (hostile / ".env").write_text("ALKERA_API_URL=https://attacker.example\n", encoding="utf-8")
    monkeypatch.delenv("ALKERA_API_URL", raising=False)
    monkeypatch.chdir(hostile)

    assert config._looks_like_source_checkout(hostile)  # it does look like one…
    assert config.CliSettings().alkera_api_url != "https://attacker.example"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows refuses to delete the CWD")
def test_settings_load_when_the_cwd_no_longer_exists(
    fake_checkout: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Deciding per load means `Path.cwd()` runs on EVERY settings load — and a
    long-lived process (the daemon, a pytest session) outlives directories: a build
    step or a tmp-dir cleanup can delete the one it was started in. Settings sit on
    nearly every code path, so an unreadable CWD has to degrade to "no dotenv layer"
    rather than raise `FileNotFoundError` out of `get_settings()`. Fail-closed, too:
    a CWD we cannot read cannot be shown to be inside the checkout."""
    from alkera_cli.host import config

    doomed = tmp_path / "deleted-under-us"
    doomed.mkdir()
    monkeypatch.chdir(doomed)
    shutil.rmtree(doomed)

    assert config._dotenv_files() == ()
    assert config.CliSettings().alkera_api_url == config.DEFAULT_API_URL
