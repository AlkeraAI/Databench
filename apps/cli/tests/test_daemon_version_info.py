"""What a daemon calls itself: the package version, plus the build or release
it runs when the environment names one, always short enough for the heartbeat.

A node once ran a binary three release labels older than the one its rig had
staged, and nothing it reported said so: its heartbeat carried the package
version alone. The report now carries the build id the binary was compiled
with or, on a provisioned node, the release label its bootstrap installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli import __version__
from alkera_cli.cloud.service import MirrorSettings
from alkera_cli.host.version_info import MAX_LENGTH, daemon_version


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param({}, __version__, id="source-run-reports-the-package-version"),
        pytest.param(
            {"ALKERA_BUILD_ID": "537229686"},
            f"{__version__} (build 537229686)",
            id="a-compiled-binary-names-its-build",
        ),
        pytest.param(
            {"ALKERA_RELEASE_VERSION": "b13"},
            f"{__version__} (build b13)",
            id="a-provisioned-node-names-the-release-it-installed",
        ),
        pytest.param(
            {"ALKERA_BUILD_ID": "537229686", "ALKERA_RELEASE_VERSION": "b13"},
            f"{__version__} (build 537229686)",
            id="the-compiled-build-id-wins-over-the-release-label",
        ),
        pytest.param(
            {"ALKERA_BUILD_ID": "", "ALKERA_RELEASE_VERSION": "b13"},
            f"{__version__} (build b13)",
            id="an-empty-build-id-falls-through-to-the-release",
        ),
        pytest.param(
            {"ALKERA_BUILD_ID": "", "ALKERA_RELEASE_VERSION": ""},
            __version__,
            id="empty-values-are-no-build",
        ),
    ],
)
def test_the_daemon_names_its_build(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
) -> None:
    for key in ("ALKERA_BUILD_ID", "ALKERA_RELEASE_VERSION"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert daemon_version() == expected


def test_a_long_build_id_is_cut_to_fit_the_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALKERA_RELEASE_VERSION", raising=False)
    monkeypatch.setenv("ALKERA_BUILD_ID", "x" * 100)
    reported = daemon_version()
    assert len(reported) <= MAX_LENGTH
    assert reported.startswith(f"{__version__} (build x") and reported.endswith(")")


def test_the_mirror_settings_report_the_release_the_node_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The heartbeat's ``daemon_version`` is read from the settings' default,
    resolved when the settings are built (systemd sets the node's environment
    before the daemon starts), not when the module was imported."""
    monkeypatch.delenv("ALKERA_BUILD_ID", raising=False)
    monkeypatch.setenv("ALKERA_RELEASE_VERSION", "b13")
    settings = MirrorSettings(
        api_url="http://api", token="t", project_dir=tmp_path, machine_name="n"
    )
    assert settings.daemon_version == f"{__version__} (build b13)"
    monkeypatch.delenv("ALKERA_RELEASE_VERSION")
    bare = MirrorSettings(api_url="http://api", token="t", project_dir=tmp_path, machine_name="n")
    assert bare.daemon_version == __version__


# -- the build a compiled binary names itself --------------------------------


@pytest.mark.parametrize(
    ("baked", "env", "expected_build"),
    [
        pytest.param(
            "951dac943d65", {}, "951dac943d65", id="a-compiled-binary-names-its-baked-build"
        ),
        pytest.param(
            "951dac943d65",
            {"ALKERA_RELEASE_VERSION": "0.5.0"},
            "951dac943d65",
            id="the-baked-build-outranks-the-label-it-was-installed-under",
        ),
        pytest.param(
            "951dac943d65",
            {"ALKERA_BUILD_ID": "operator-pin"},
            "operator-pin",
            id="an-explicit-build-id-still-overrides-the-bake",
        ),
        pytest.param(
            "", {"ALKERA_RELEASE_VERSION": "0.5.0"}, "0.5.0", id="unbaked-falls-to-the-label"
        ),
        pytest.param("", {}, "", id="a-source-run-names-no-build"),
    ],
)
def test_the_baked_build_id_is_what_the_binary_reports(
    monkeypatch: pytest.MonkeyPatch, baked: str, env: dict[str, str], expected_build: str
) -> None:
    """A roll verifies a node against the build the binary IS. The label the
    node was installed under is what somebody SAID it was, so it only speaks
    when the binary carries no build of its own."""
    import alkera_cli.host.build_profile as build_profile
    from alkera_cli.host.version_info import build_id

    for key in ("ALKERA_BUILD_ID", "ALKERA_RELEASE_VERSION"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(build_profile, "BUILD_ID", baked)
    assert build_id() == expected_build
    expected = f"{__version__} (build {expected_build})" if expected_build else __version__
    assert daemon_version() == expected


def test_the_committed_profile_bakes_no_build() -> None:
    """A source checkout must never claim to be a particular compiled build."""
    import alkera_cli.host.build_profile as build_profile

    assert build_profile.BUILD_ID == ""


def test_alkera_version_json_reports_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the roll's status probe runs on the node's installed binary."""
    import json

    import alkera_cli.host.build_profile as build_profile
    from alkera_cli.main import app
    from typer.testing import CliRunner

    monkeypatch.delenv("ALKERA_BUILD_ID", raising=False)
    monkeypatch.setenv("ALKERA_RELEASE_VERSION", "0.5.0-g951dac943d65")
    monkeypatch.setattr(build_profile, "BUILD_ID", "951dac943d65")
    result = CliRunner().invoke(app, ["version", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "version": __version__,
        "build": "951dac943d65",
        "daemon_version": f"{__version__} (build 951dac943d65)",
    }


def test_plain_alkera_version_is_unchanged() -> None:
    from alkera_cli.main import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"alkera-cli {__version__}"
