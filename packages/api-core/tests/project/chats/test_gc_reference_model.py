"""The blob GC reference walk + per-chat exclude + single-blob delete.

GC re-derives its root set at every sweep by walking each persisted line for
handle-shaped references (dicts, typed lists, and JSON carried inside string
outputs), so an under-rooting bug stays repairable for logs already on disk.
The lifetime is pinned e2e in the DuckDB SQL query tests;
the hand-written lines here pin every reference shape the walk must root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alkera_core.project.directory import ProjectDirectory


def _write_chat(project: ProjectDirectory, sid: str, events: list[dict]) -> None:
    chat_dir = project.chats_path / sid
    chat_dir.mkdir(parents=True, exist_ok=True)
    (chat_dir / "chat.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )


def test_every_reference_shape_roots_through_the_sweep_walk(tmp_path: Path) -> None:
    # Every shape a log can carry: a file part, a handle dict under any key
    # (aws.s3 used result_blob, so rooting is by the 64-hex sha256 shape, never
    # the parent key), a typed references entry, a handle inside a JSON STRING
    # output (how the harness adapters persist results), and a line whose
    # TOP-LEVEL references list holds DICTS beside a handle in its output.
    # A malformed sha roots nothing, and the sweep still deletes an
    # unreferenced blob.
    project = ProjectDirectory(tmp_path / ".alkera")
    store = project.chats()
    blobs = store.blobs
    sha_file, _ = blobs.write(b"a file-part attachment")
    sha_named, _ = blobs.write(b"an s3 listing spill under result_blob")
    sha_ref, _ = blobs.write(b"a typed reference")
    sha_string, _ = blobs.write(b"a spilled result behind a string output")
    sha_handle_str, _ = blobs.write(b"a handle-spelled reference inside a string output")
    sha_dictref, _ = blobs.write(b"a dict entry in a top-level references list")
    sha_beside, _ = blobs.write(b"a handle beside a dict-shaped references list")
    sha_orphan, _ = blobs.write(b"nobody references me")

    string_output = json.dumps(
        {"truncated": True, "blob": {"sha256": sha_string, "size": 10, "media_type": "x"}}
    )
    # No "sha256" substring anywhere in this string: only the handle spelling,
    # so a parse guard keyed to one spelling would skip it unparsed.
    handle_string_output = json.dumps({"references": [{"handle": sha_handle_str}]})
    dict_output = {
        "result_blob": {"sha256": sha_named, "size": 1, "media_type": "x"},
        "junk": {"sha256": "not-a-real-sha"},
    }
    beside_output = {"blob": {"sha256": sha_beside, "size": 1, "media_type": "x"}}
    _write_chat(
        project,
        "c1",
        [
            {"event_type": "part.created", "part": {"type": "file", "sha256": sha_file}},
            {"event_type": "part.created", "part": {"type": "tool", "output": dict_output}},
            {"event_type": "part.created", "part": {"references": [{"handle": sha_ref}]}},
            {"event_type": "part.created", "part": {"type": "tool", "output": string_output}},
            {
                "event_type": "part.created",
                "part": {"type": "tool", "output": handle_string_output},
            },
            {
                "event_type": "part.created",
                "references": [{"handle": sha_dictref}],
                "part": {"type": "tool", "output": beside_output},
            },
        ],
    )

    report = store.gc(grace_period_seconds=0)

    assert blobs.exists(sha_file), "file-part attachment must survive"
    assert blobs.exists(sha_named), "a handle under a nonstandard key must survive"
    assert blobs.exists(sha_ref), "typed reference must survive"
    assert blobs.exists(sha_string), "a handle carried in a string output must survive"
    assert blobs.exists(sha_handle_str), "a handle-spelled reference in a string must survive"
    assert blobs.exists(sha_dictref), "a dict entry in a top-level references list must survive"
    assert blobs.exists(sha_beside), "a handle beside a dict-shaped references list must survive"
    assert not blobs.exists(sha_orphan), "an unreferenced blob past grace is swept"
    assert report.deleted == 1


def test_a_pathological_string_output_persists_and_still_roots(tmp_path: Path) -> None:
    # JSON nested past the parser's recursion limit inside an output STRING: a
    # blown parse in the reference walk would abort the whole sweep. The line
    # must land, the handle in the sibling input must root, and the handle
    # carried INSIDE the unparseable string must root through the raw hex scan
    # the failed parse falls back to.
    from datetime import UTC, datetime

    from alkera_core.schemas.chat import ToolCallUpdate

    project = ProjectDirectory(tmp_path / ".alkera")
    store = project.chats()
    blobs = store.blobs
    sha_real, _ = blobs.write(b"the real handle beside the pathological string")
    sha_inside, _ = blobs.write(b"the handle carried inside the unparseable string")
    # 50,000 levels: the C scanner absorbs a few thousand, so the depth must be
    # far past it for json.loads to raise inside the walk. Pinned here so a
    # roomier future parser flags the fixture as inert instead of passing.
    deep = '{"sha256": "' + sha_inside + '", "deep": ' + "[" * 50_000 + "]" * 50_000 + "}"
    with pytest.raises(RecursionError):
        json.loads(deep)
    with store.create(session_id="s1") as chat:
        chat.append_event(
            ToolCallUpdate(
                event_id="ev-1",
                time=datetime.now(UTC),
                session_id="s1",
                tool_call_id="t1",
                status="completed",
                input={"blob": {"sha256": sha_real, "size": 1, "media_type": "x"}},
                output=deep,
            )
        )
    lines = [
        json.loads(line)
        for line in (project.chats_path / "s1" / "chat.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(line.get("tool_call_id") == "t1" for line in lines), "the event landed"
    store.gc(grace_period_seconds=0)
    assert blobs.exists(sha_real), "the real handle beside the deep string must survive"
    assert blobs.exists(sha_inside), "a handle inside the unparseable string must survive"


def test_referenced_blobs_excludes_a_session(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    store = project.chats()
    blobs = store.blobs
    shared, _ = blobs.write(b"shared by two chats")
    only_c1, _ = blobs.write(b"only c1")

    spill = lambda sha: {  # noqa: E731 — tiny local builder
        "event_type": "part.created",
        "part": {"type": "tool", "output": {"blob": {"sha256": sha, "size": 1, "media_type": "x"}}},
    }
    _write_chat(project, "c1", [spill(shared), spill(only_c1)])
    _write_chat(project, "c2", [spill(shared)])

    # Excluding c1, the shared blob is still referenced (by c2) but only_c1 isn't.
    others = store.referenced_blobs(exclude_session_id="c1")
    assert shared in others
    assert only_c1 not in others

    # Excluding both leaves nothing.
    assert store.referenced_blobs(exclude_session_id="c2") == {shared, only_c1}


def test_blobstore_delete(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    blobs = project.blobs()
    sha, _ = blobs.write(b"deletable")
    assert blobs.exists(sha)
    assert blobs.delete(sha) is True
    assert not blobs.exists(sha)
    # Idempotent: deleting an absent blob is False, not an error.
    assert blobs.delete(sha) is False
    # A malformed sha is a programming error, surfaced loudly.
    with pytest.raises(ValueError, match="sha256"):
        blobs.delete("not-a-sha")


def test_a_u_escaped_sha_inside_a_string_output_still_roots(tmp_path: Path) -> None:
    # Every hex digit of the sha spelled as its own \u00XX escape, so the raw
    # text carries no contiguous run of hex characters for a naive scan to
    # catch — the walk must decode the escapes to find the handle.
    project = ProjectDirectory(tmp_path / ".alkera")
    store = project.chats()
    blobs = store.blobs
    sha_escaped, _ = blobs.write(b"a handle spelled entirely in u-escapes")
    sha_orphan, _ = blobs.write(b"nobody references me")

    escaped_hex = "".join(f"\\u{ord(c):04x}" for c in sha_escaped)
    string_output = '{"sha256": "' + escaped_hex + '"}'
    _write_chat(
        project,
        "c1",
        [
            {"event_type": "part.created", "part": {"type": "tool", "output": string_output}},
        ],
    )

    store.gc(grace_period_seconds=0)

    assert blobs.exists(sha_escaped), "a \\u-escaped sha inside a string output must survive"
    assert not blobs.exists(sha_orphan), "an unreferenced blob past grace is swept"


def test_directly_nested_deep_arrays_do_not_abort_the_sweep(tmp_path: Path) -> None:
    # Real JSON structure — not string-wrapped — nested 50,000 levels deep,
    # embedded directly in the line's own JSON. A recursive line parser or
    # walker blows the stack before it ever reaches the sibling line.
    project = ProjectDirectory(tmp_path / ".alkera")
    store = project.chats()
    blobs = store.blobs
    sha_sibling, _ = blobs.write(b"a normal sibling reference beside the deep line")

    deep = "[" * 50_000 + "]" * 50_000
    with pytest.raises(RecursionError):
        json.loads('{"junk": ' + deep + "}")

    chat_dir = project.chats_path / "c1"
    chat_dir.mkdir(parents=True, exist_ok=True)
    deep_line = '{"event_type": "part.created", "part": {"junk": ' + deep + "}}"
    sibling_line = json.dumps(
        {"event_type": "part.created", "part": {"type": "file", "sha256": sha_sibling}}
    )
    (chat_dir / "chat.jsonl").write_text(deep_line + "\n" + sibling_line + "\n", encoding="utf-8")

    store.gc(grace_period_seconds=0)  # must not raise past the pathological line

    assert blobs.exists(sha_sibling), "a normal sibling reference must survive the sweep"


def test_a_bare_sha_string_inside_a_list_roots(tmp_path: Path) -> None:
    # No dict wrapper, no "sha256" key — the sha sits directly as a list
    # element, the shape an "attachments": [<sha>, ...] field uses.
    project = ProjectDirectory(tmp_path / ".alkera")
    store = project.chats()
    blobs = store.blobs
    sha_listed, _ = blobs.write(b"a bare sha sitting directly in a list")
    sha_orphan, _ = blobs.write(b"nobody references me")

    _write_chat(
        project,
        "c1",
        [
            {
                "event_type": "part.created",
                "part": {"type": "tool", "attachments": [sha_listed]},
            },
        ],
    )

    store.gc(grace_period_seconds=0)

    assert blobs.exists(sha_listed), "a bare sha256 string inside a list must survive"
    assert not blobs.exists(sha_orphan), "an unreferenced blob past grace is swept"


def test_collect_blob_references_rejects_a_malformed_near_sha() -> None:
    # Both look like a sha at a glance: one hex character short, and 64
    # characters where one isn't a hex digit. Neither must root anything.
    from alkera_core.project.chats.references import collect_blob_references

    too_short = "a" * 63
    wrong_alphabet = "a" * 63 + "g"

    assert collect_blob_references(too_short) == set()
    assert collect_blob_references(wrong_alphabet) == set()
