"""The generated files the web reads are what the registry says today, and the
permission modes agree with the stances the server accepts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest
from alkera_core.permission_presentation import (
    MODE_RANK,
    PERMISSION_MODES,
    WRITING_MODES,
    mode_words,
)
from alkera_core.permission_presentation.export import (
    is_current,
    outputs,
    registry_document,
    render_ts,
    write,
)
from alkera_core.schemas.objects.specs import CloudPermissionMode


@pytest.mark.parametrize("path", list(outputs()), ids=lambda path: path.name)
def test_the_committed_copy_is_current(path: Path) -> None:
    """A registry change without ``make gen-tool-manifest`` would ship a web
    card that disagrees with the Slack card. This fails first."""
    assert is_current(path, outputs()[path])


def test_a_crlf_checkout_of_a_current_file_is_still_current(tmp_path: Path) -> None:
    """The Windows runners check out with ``core.autocrlf=true``: the committed
    LF file arrives as CRLF, and it is still the file the generator writes."""
    content = render_ts()
    checkout = tmp_path / "permissionPresentation.ts"
    checkout.write_bytes(content.replace("\n", "\r\n").encode("utf-8"))
    assert is_current(checkout, content)


def test_a_real_change_is_stale_whatever_the_line_endings(tmp_path: Path) -> None:
    content = render_ts()
    changed = content.replace("Run this command?", "Run this?")
    assert changed != content
    for newline in ("\n", "\r\n"):
        checkout = tmp_path / "permissionPresentation.ts"
        checkout.write_bytes(changed.replace("\n", newline).encode("utf-8"))
        assert not is_current(checkout, content)


def test_the_generator_writes_lf_on_every_platform(tmp_path: Path) -> None:
    target = tmp_path / "out.ts"
    write(target, "a\nb\n")
    assert target.read_bytes() == b"a\nb\n"


def test_the_generated_registry_carries_every_presenter_and_mode() -> None:
    document = registry_document()
    assert json.loads(json.dumps(document)) == document
    assert [mode["value"] for mode in document["modes"]] == [m.value for m in PERMISSION_MODES]


def test_the_modes_are_exactly_the_stances_the_server_accepts() -> None:
    """Bypass included: a stance one surface offers and another does not is the
    parity defect this list exists to prevent."""
    assert sorted(mode.value for mode in PERMISSION_MODES) == sorted(get_args(CloudPermissionMode))


@pytest.mark.parametrize(
    ("word", "mode"),
    [
        pytest.param("ask", "default", id="alias"),
        pytest.param("default", "default", id="value"),
        pytest.param("bypass", "bypass", id="bypass"),
        pytest.param("read-only", "read_only", id="hyphenated"),
        pytest.param("read_only", "read_only", id="underscored"),
    ],
)
def test_mode_words(word: str, mode: str) -> None:
    assert mode_words()[word] == mode


def test_no_word_names_two_stances() -> None:
    words: list[str] = []
    for mode in PERMISSION_MODES:
        words.extend([mode.value, *mode.aliases])
    assert len(words) == len(set(words))


def test_the_ranks_order_the_stances_strictest_first() -> None:
    """Only the order matters, and it is a strict one: two stances sharing a rank
    would let a template neither narrow nor keep a reader's stance cleanly."""
    assert sorted(MODE_RANK, key=MODE_RANK.__getitem__) == [
        "read_only",
        "plan",
        "default",
        "auto",
        "bypass",
    ]
    assert len(set(MODE_RANK.values())) == len(MODE_RANK)


def test_every_writing_stance_ranks_above_every_read_only_one() -> None:
    """A template may only narrow a chat's stance. Were a writing stance ranked
    below a read-only one, narrowing would hand a read-only reader a stance that
    runs code."""
    writing = [MODE_RANK[m] for m in MODE_RANK if m in WRITING_MODES]
    reading = [MODE_RANK[m] for m in MODE_RANK if m not in WRITING_MODES]
    assert min(writing) > max(reading)
    assert frozenset({"default", "auto", "bypass"}) == WRITING_MODES
