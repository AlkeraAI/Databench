"""Guards that the repo-root `VERSION.txt` (the binary/CLI semver, consumed by
the binary build for its version-info flags) stays in sync
with the package version reported by `alkera --version` and the daemon.

`VERSION.txt` is the binary version line; it is deliberately independent of the
editor extension's own version (see the build scripts' header comments).
"""

from __future__ import annotations

from pathlib import Path

from alkera_cli import __version__

# apps/cli/tests/test_version.py -> repo root is 3 levels up.
_REPO_ROOT = Path(__file__).resolve().parents[4]


def test_version_txt_matches_package_version() -> None:
    version_txt = (_REPO_ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    assert version_txt == __version__, (
        f"VERSION.txt ({version_txt!r}) must match alkera_cli.__version__ "
        f"({__version__!r}); bump both together."
    )


def test_get_version_returns_dunder() -> None:
    from alkera_cli.host.version import get_version

    assert get_version() == __version__


def test_bundled_version_path_resolves_in_source_checkout() -> None:
    """In a source checkout the resolver walks up to the repo-root VERSION.txt
    (the frozen path — ``<extract>/VERSION.txt`` — is exercised by the binary's
    own smoke test, not unit-testable without a Nuitka build)."""
    from alkera_cli.host.version import bundled_version_path

    path = bundled_version_path()
    assert path is not None
    assert path.name == "VERSION.txt"
    assert path == _REPO_ROOT / "VERSION.txt"


def test_bundled_version_matches_dunder() -> None:
    from alkera_cli.host.version import bundled_version

    assert bundled_version() == __version__


def test_version_flag_exits_zero_and_prints_version() -> None:
    """`alkera --version` is an eager top-level flag (used by the release
    portability smoke + the brew formula test); it must exit 0 and print the
    version, distinct from the `alkera version` subcommand."""
    from alkera_cli.main import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output


def test_version_callback_exits_only_when_flag_set() -> None:
    import pytest
    import typer
    from alkera_cli.main import _version_callback

    _version_callback(False)  # no-op when the flag is absent
    with pytest.raises(typer.Exit):
        _version_callback(True)
