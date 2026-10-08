"""The corpus resolver: which version directory a regenerate lands on."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from alkera_core.versioning.corpus import corpus_key, resolve_corpus_dir


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        pytest.param(PureWindowsPath("events\\turn.json"), "events/turn.json", id="windows"),
        pytest.param(PurePosixPath("events/turn.json"), "events/turn.json", id="posix"),
        pytest.param(PureWindowsPath("turn.json"), "turn.json", id="flat"),
    ],
)
def test_a_committed_file_is_keyed_the_way_a_payload_names_it(
    relative: PurePosixPath | PureWindowsPath, expected: str
) -> None:
    # A payload spells its names with slashes; the committed side must too,
    # or nothing matches on a host whose paths use backslashes.
    assert corpus_key(relative) == expected


def _write(root: Path, version: str, files: dict[str, bytes]) -> Path:
    directory = root / f"v{version.replace('.', '_')}"
    for name, blob in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return directory


def test_a_matching_nested_corpus_is_the_stamped_directory(tmp_path: Path) -> None:
    payload = {"events/turn.json": b"{}\n", "meta.json": b"[]\n"}
    committed = _write(tmp_path, "1.2.0", payload)
    assert resolve_corpus_dir(tmp_path, "1.2.0", payload) == committed


def test_a_differing_corpus_lands_on_the_next_minor_and_leaves_the_old_one(
    tmp_path: Path,
) -> None:
    committed = _write(tmp_path, "1.2.0", {"events/turn.json": b"{}\n"})
    resolved = resolve_corpus_dir(tmp_path, "1.2.0", {"events/turn.json": b'{"a": 1}\n'})
    assert resolved == tmp_path / "v1_3_0"
    assert (committed / "events" / "turn.json").read_bytes() == b"{}\n"
