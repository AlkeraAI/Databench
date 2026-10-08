"""A spec on disk is untrusted: anyone who can write the workspace writes it,
and a recreate turns its values into requirement lines, arguments and a
``.pth`` file. Each hostile value is refused when the spec is read, so it
never reaches a plan; a folder that leaves the workspace through a link is
never counted as present."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.environment import InvalidSpecError, load_spec, read_spec
from alkera_cli.environment.probe import probe_in_process
from alkera_cli.environment.recreate import TargetRead, Tools, plan_recreate, target_state
from alkera_cli.environment.spec import SPEC_FILENAME, EnvironmentSpec, PackageSpec

needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")

BASE: dict[str, Any] = {
    "schema_version": "1.0.0",
    "env_kind": "venv",
    "python": {"version": "3.12.4", "implementation": "cpython"},
    "packages": [
        {"name": "alpha", "version": "1.0"},
        {"name": "mylib", "version": "0.1", "source": "editable", "path": "libs/mylib"},
    ],
    "indexes": [{"url": "https://pkgs.example/simple", "kind": "index"}],
    "conda": {"channels": ["conda-forge"], "specs": ["numpy >=1.2"], "packages": []},
    "path_entries": ["tools"],
}


def _with(path: list[Any], value: Any) -> dict[str, Any]:
    data = copy.deepcopy(BASE)
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return data


HOSTILE = [
    pytest.param(["packages", 1, "path"], "../../../home/victim/proj", id="editable-escapes-root"),
    pytest.param(["packages", 1, "path"], "/etc", id="editable-absolute"),
    pytest.param(["packages", 1, "path"], "C:/Windows", id="editable-drive"),
    pytest.param(["packages", 1, "path"], "libs\\..\\..\\x", id="editable-backslash"),
    pytest.param(["packages", 1, "path"], "libs/x\n-e /etc", id="editable-newline"),
    pytest.param(["packages", 1, "path"], "-e/etc", id="editable-option"),
    pytest.param(["packages", 1, "subdirectory"], "../..", id="subdirectory-escapes"),
    pytest.param(
        ["packages", 0, "name"],
        "alpha\n--index-url https://evil.example/simple\n-e /etc",
        id="name-injects-options",
    ),
    pytest.param(["packages", 0, "name"], "-r/etc/passwd", id="name-is-an-option"),
    pytest.param(["packages", 0, "version"], "1.0 --hash=sha256:00", id="version-with-space"),
    pytest.param(["packages", 0, "version"], "1.0\n-e /etc", id="version-newline"),
    pytest.param(["packages", 0, "hashes"], ["sha256:aa\n--no-deps"], id="hash-newline"),
    pytest.param(["packages", 0, "url"], "https://x.example/a.whl\n-e /etc", id="url-newline"),
    pytest.param(["indexes", 0, "url"], "https://ok.example\n-e /etc", id="index-newline"),
    pytest.param(["indexes", 0, "url"], "--trusted-host=evil", id="index-is-an-option"),
    pytest.param(
        ["path_entries"], ["tools\nimport os;os.system('touch /tmp/pwned')"], id="pth-newline-code"
    ),
    pytest.param(["path_entries"], ["../outside"], id="pth-escapes-root"),
    pytest.param(["conda", "specs"], ["--override-channels"], id="conda-spec-option"),
    pytest.param(["conda", "channels"], ["-c evil"], id="conda-channel-option"),
    pytest.param(["conda", "specs"], ["numpy\n--prefix /"], id="conda-spec-newline"),
]


@pytest.mark.parametrize("path,value", HOSTILE)
def test_a_hostile_value_makes_the_spec_unusable(
    tmp_path: Path, path: list[Any], value: Any
) -> None:
    (tmp_path / SPEC_FILENAME).write_text(json.dumps(_with(path, value)), encoding="utf-8")
    assert read_spec(tmp_path) is None
    with pytest.raises(InvalidSpecError, match="not a valid environment spec"):
        load_spec(tmp_path)


def test_the_untouched_base_is_valid(tmp_path: Path) -> None:
    (tmp_path / SPEC_FILENAME).write_text(json.dumps(BASE), encoding="utf-8")
    assert load_spec(tmp_path).package("mylib") is not None


@needs_posix
def test_an_editable_source_linked_out_of_the_workspace_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "libs").mkdir(parents=True)
    os.symlink("/", root / "libs" / "mylib")
    (root / "tools").mkdir()
    probe = probe_in_process(str(root), paths=("libs/mylib", "tools"))
    assert probe.paths == {"libs/mylib": False, "tools": True}

    spec = EnvironmentSpec(
        packages=[PackageSpec(name="mylib", version="0.1", source="editable", path="libs/mylib")]
    )
    plan = plan_recreate(
        spec,
        target_state(TargetRead(probe=probe, exists=False)),
        target_env=str(tmp_path / "env"),
        target_root=str(root),
        tools=Tools(uv="uv"),
    )
    assert [(g.kind, g.name) for g in plan.gaps] == [("local_source_missing", "mylib")]
    assert "install_local" not in [s.purpose for s in plan.steps]


@needs_posix
def test_a_workspace_folder_linked_to_a_sibling_inside_is_present(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "real").mkdir(parents=True)
    os.symlink(root / "real", root / "alias")
    assert probe_in_process(str(root), paths=("alias",)).paths == {"alias": True}


@needs_posix
@pytest.mark.parametrize("name", [SPEC_FILENAME, "ENVIRONMENT.md"])
def test_a_workspace_file_hard_linked_to_a_file_elsewhere_is_not_read(
    tmp_path: Path, name: str
) -> None:
    """A hard link is a regular file with no link to refuse, so the box's root
    would read whatever host file the chat linked in. A regular file with a
    second name is refused like a symlink; the same content under one name
    reads."""
    from alkera_cli.environment.files import read_workspace_text

    root = tmp_path / "ws"
    root.mkdir()
    elsewhere = tmp_path / "host-secret"
    elsewhere.write_text(json.dumps(BASE), encoding="utf-8")
    os.link(elsewhere, root / name)

    assert read_workspace_text(root, name, max_bytes=1 << 20) is None
    if name == SPEC_FILENAME:
        assert read_spec(root) is None

    os.unlink(root / name)
    (root / name).write_text(json.dumps(BASE), encoding="utf-8")
    assert read_workspace_text(root, name, max_bytes=1 << 20) == json.dumps(BASE)
