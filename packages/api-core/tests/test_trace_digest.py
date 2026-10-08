"""Trace digest: golden values, tamper detection, fixture lineage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_core.project.chats.trace import (
    DIGEST_FILENAME,
    TraceVerdict,
    compute_trace_digest,
    pin_trace,
    pin_trace_if_changed,
    read_trace_digest,
    verify_trace_file,
)
from alkera_core.schemas.chat.trace import TraceDigest

_FIXTURES = Path(__file__).parent / "fixtures" / "trace"

_CHAT = b'{"type":"message"}\n'
_DECISIONS = b'{"decision":"allow"}\n'
_GOLDEN_COMBINED = "13f8f89184e560e570c6e4f5996adeede9ed9300f4d3ea06fec42fae62c683ed"


def _seed(chat_dir: Path) -> None:
    chat_dir.mkdir(parents=True, exist_ok=True)
    (chat_dir / "chat.jsonl").write_bytes(_CHAT)
    (chat_dir / "decisions.jsonl").write_bytes(_DECISIONS)


def test_combined_matches_golden(tmp_path: Path) -> None:
    _seed(tmp_path)
    digest = compute_trace_digest(tmp_path, "s1")
    assert digest.combined == _GOLDEN_COMBINED
    assert set(digest.files) == {"chat.jsonl", "decisions.jsonl"}


def test_any_edit_changes_combined(tmp_path: Path) -> None:
    _seed(tmp_path)
    (tmp_path / "chat.jsonl").write_bytes(_CHAT[:-2] + b"X\n")
    assert compute_trace_digest(tmp_path, "s1").combined != _GOLDEN_COMBINED


def test_a_new_file_changes_combined(tmp_path: Path) -> None:
    _seed(tmp_path)
    (tmp_path / "cost_ledger.jsonl").write_bytes(b'{"entry_id":"e1"}\n')
    assert compute_trace_digest(tmp_path, "s1").combined != _GOLDEN_COMBINED


def test_pin_round_trips_and_detects_tamper(tmp_path: Path) -> None:
    _seed(tmp_path)
    pinned = pin_trace(tmp_path, "s1")
    assert (tmp_path / DIGEST_FILENAME).is_file()
    back = read_trace_digest(tmp_path)
    assert back is not None
    assert back.combined == pinned.combined

    (tmp_path / "decisions.jsonl").write_bytes(b'{"decision":"reject"}\n')
    assert compute_trace_digest(tmp_path, "s1").combined != back.combined


def test_unreadable_digest_reads_as_none(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / DIGEST_FILENAME).write_text("{not json")
    assert read_trace_digest(tmp_path) is None


@pytest.mark.parametrize("fixture", sorted(_FIXTURES.glob("v*.json")), ids=lambda p: p.stem)
def test_every_historical_fixture_loads(fixture: Path) -> None:
    digest = TraceDigest.model_validate(json.loads(fixture.read_text()))
    assert digest.schema_version == TraceDigest.SCHEMA_VERSION
    assert digest.combined


def test_the_digest_records_how_far_each_hash_reaches(tmp_path: Path) -> None:
    """The logs are append-only, so the hash alone cannot be checked once the
    next box has written a line: the length says which prefix it covers.
    Recording it leaves ``combined`` — the value audit events carry — as it
    was, so a pin written before lengths existed still matches."""
    _seed(tmp_path)
    digest = compute_trace_digest(tmp_path, "s1")
    assert digest.sizes == {"chat.jsonl": len(_CHAT), "decisions.jsonl": len(_DECISIONS)}
    assert digest.combined == _GOLDEN_COMBINED


def _legacy_pin(chat_dir: Path) -> None:
    """A digest as a writer before 1.1.0 left it: hashes and no lengths."""
    digest = compute_trace_digest(chat_dir, "s1")
    (chat_dir / DIGEST_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "session_id": "s1",
                "algorithm": "sha256",
                "files": digest.files,
                "combined": digest.combined,
            }
        )
    )


@pytest.mark.parametrize(
    ("after_pin", "verdict"),
    [
        pytest.param(lambda p: None, TraceVerdict.INTACT, id="untouched"),
        pytest.param(
            lambda p: (p / "chat.jsonl").open("ab").write(b'{"type":"later"}\n'),
            TraceVerdict.INTACT,
            id="appended-by-the-next-box",
        ),
        pytest.param(
            lambda p: (p / "chat.jsonl").write_bytes(b'{"type":"forged!"}\n'),
            TraceVerdict.TAMPERED,
            id="rewritten-at-the-same-length",
        ),
        pytest.param(
            lambda p: (p / "chat.jsonl").write_bytes(b'{"type":"forged!"}\n' + _CHAT),
            TraceVerdict.TAMPERED,
            id="a-line-inserted-before-the-pinned-bytes",
        ),
        pytest.param(
            lambda p: (p / "chat.jsonl").write_bytes(_CHAT[:-4]),
            TraceVerdict.TAMPERED,
            id="truncated-below-the-pinned-length",
        ),
        pytest.param(
            lambda p: (p / "chat.jsonl").unlink(),
            TraceVerdict.TAMPERED,
            id="gone-while-the-digest-names-it",
        ),
    ],
)
def test_the_transcript_is_checked_against_the_prefix_the_pin_covers(
    tmp_path: Path, after_pin: object, verdict: TraceVerdict
) -> None:
    _seed(tmp_path)
    pin_trace(tmp_path, "s1")
    after_pin(tmp_path)  # type: ignore[operator]
    assert verify_trace_file(tmp_path, "chat.jsonl") is verdict


def test_a_log_the_digest_does_not_name_is_unpinned_not_tampered(tmp_path: Path) -> None:
    """A cost ledger that first appears after the pin is new, not forged."""
    _seed(tmp_path)
    pin_trace(tmp_path, "s1")
    (tmp_path / "cost_ledger.jsonl").write_bytes(b'{"entry_id":"e1"}\n')
    assert verify_trace_file(tmp_path, "cost_ledger.jsonl") is TraceVerdict.UNPINNED
    assert verify_trace_file(tmp_path, "chat.jsonl") is TraceVerdict.INTACT


def test_no_digest_is_unpinned(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert verify_trace_file(tmp_path, "chat.jsonl") is TraceVerdict.UNPINNED
    (tmp_path / DIGEST_FILENAME).write_text("{not json")
    assert verify_trace_file(tmp_path, "chat.jsonl") is TraceVerdict.UNPINNED


@pytest.mark.parametrize(
    ("after_pin", "verdict"),
    [
        pytest.param(lambda p: None, TraceVerdict.INTACT, id="whole-file-still-matches"),
        pytest.param(
            lambda p: (p / "chat.jsonl").open("ab").write(b'{"type":"later"}\n'),
            TraceVerdict.UNPINNED,
            id="grown-since-which-an-old-pin-cannot-tell-from-an-edit",
        ),
        pytest.param(
            lambda p: (p / "chat.jsonl").write_bytes(b'{"type":"forged!"}\n'),
            TraceVerdict.UNPINNED,
            id="rewritten-which-an-old-pin-cannot-tell-from-an-append",
        ),
        pytest.param(
            lambda p: (p / "chat.jsonl").unlink(),
            TraceVerdict.TAMPERED,
            id="gone-is-tampered-even-for-an-old-pin",
        ),
    ],
)
def test_a_digest_without_lengths_can_only_vouch_for_the_whole_file(
    tmp_path: Path, after_pin: object, verdict: TraceVerdict
) -> None:
    _seed(tmp_path)
    _legacy_pin(tmp_path)
    after_pin(tmp_path)  # type: ignore[operator]
    assert verify_trace_file(tmp_path, "chat.jsonl") is verdict


def test_pin_if_changed_leaves_a_current_digest_alone(tmp_path: Path) -> None:
    """The box pins before every checkpoint push; a chat whose logs have not
    moved must not get a new digest — and so a new version on the drive — on
    every beat."""
    _seed(tmp_path)
    first = pin_trace(tmp_path, "s1", now=datetime(2026, 9, 1, tzinfo=UTC))
    before = (tmp_path / DIGEST_FILENAME).read_bytes()
    again = pin_trace_if_changed(tmp_path, "s1", now=datetime(2026, 9, 2, tzinfo=UTC))
    assert again.created_at == first.created_at
    assert (tmp_path / DIGEST_FILENAME).read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            lambda p: (p / "chat.jsonl").open("ab").write(b'{"type":"later"}\n'), id="a-log-grew"
        ),
        pytest.param(
            lambda p: (p / "cost_ledger.jsonl").write_bytes(b'{"entry_id":"e1"}\n'),
            id="a-log-appeared",
        ),
        pytest.param(_legacy_pin, id="the-pin-predates-lengths"),
        pytest.param(lambda p: (p / DIGEST_FILENAME).unlink(), id="the-pin-is-gone"),
    ],
)
def test_pin_if_changed_rewrites_a_digest_that_no_longer_covers_the_logs(
    tmp_path: Path, change: object
) -> None:
    _seed(tmp_path)
    pin_trace(tmp_path, "s1", now=datetime(2026, 9, 1, tzinfo=UTC))
    change(tmp_path)  # type: ignore[operator]
    pinned = pin_trace_if_changed(tmp_path, "s1", now=datetime(2026, 9, 2, tzinfo=UTC))
    back = read_trace_digest(tmp_path)
    assert back is not None
    assert back.created_at == datetime(2026, 9, 2, tzinfo=UTC)
    assert back.sizes == pinned.sizes == compute_trace_digest(tmp_path, "s1").sizes
    assert verify_trace_file(tmp_path, "chat.jsonl") is TraceVerdict.INTACT


def test_pin_if_changed_does_not_trust_another_sessions_digest(tmp_path: Path) -> None:
    _seed(tmp_path)
    pin_trace(tmp_path, "other-session")
    assert pin_trace_if_changed(tmp_path, "s1").session_id == "s1"
    back = read_trace_digest(tmp_path)
    assert back is not None and back.session_id == "s1"
