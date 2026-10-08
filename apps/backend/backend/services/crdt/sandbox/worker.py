"""The Loro sandbox worker: ``python -m backend.services.crdt.sandbox.worker``.

Reads request frames on stdin and answers each with one response frame on
stdout, in order, one at a time. It is the only process that imports Loro, so a
Loro build that aborts on hostile bytes ends this process and nothing else: the
pool sees the pipe close, kills what is left, starts a fresh worker and the
backend loads the document again.

On start it refuses the packages its production venv does not install
(``REFUSED_IMPORTS``), caps its own address space and turns core dumps off, then warms
every document type it serves (``core.warm_all``) before it reads its first
frame, so its first request costs what every later one does; the ping that
admits it reports how the warm-up went. A runaway
decode is a refused allocation rather than a host under memory pressure, and
an abort leaves no core file with document bytes in it. Nothing
but frames is ever written to stdout: ``print`` and stray library output go to
stderr, which the pool forwards to the log.

Every response header carries ``ok``. A refused request is
``{ok: false, code, message}``; a request naming a document the cache does not
hold at the expected position is ``{ok: true, need: true}``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, BinaryIO

# Every module that registers a document type's strategy is imported here,
# so the worker serves each type the backend names (the notebook strategy
# registers itself; its operations live beside it).
from backend.services.crdt.sandbox import core, notebook, notebook_ops
from backend.services.crdt.sandbox.protocol import (
    MAX_BLOB_BYTES,
    MAX_BLOBS,
    MAX_HEADER_BYTES,
    MAX_PEERS,
    Frame,
    FrameError,
    encode_frame,
    encode_projection,
)

#: The strategies this worker serves beyond the lane's own text types.
STRATEGIES: tuple[core.DocStrategy, ...] = (notebook.NOTEBOOK,)

#: Packages the worker never imports, even where the interpreter has them.
#: The production worker's venv installs ``alkera-notebook`` without its
#: ``duckdb`` extra, so a SQL cell's references are found by sqlglot alone.
#: A venv that does hold DuckDB (the workspace one tests and local dev run)
#: would otherwise start a DuckDB database to split the statements, with one
#: thread per core: on a many-core host their stacks and allocator arenas
#: take most of the address-space cap and the next large frame is a
#: ``MemoryError``.
REFUSED_IMPORTS: tuple[str, ...] = ("duckdb",)


class _RefuseImports:
    """A meta-path finder that answers every name in ``names`` (and every
    submodule of one) as not installed, for an import and for
    ``importlib.util.find_spec`` alike."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self._names = names

    def find_spec(self, name: str, path: object = None, target: object = None) -> None:
        root = name.partition(".")[0]
        if root in self._names:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)


def _refuse_imports(names: tuple[str, ...]) -> None:
    loaded = sorted(name for name in names if name in sys.modules)
    if loaded:
        raise RuntimeError(f"already imported before the worker could refuse them: {loaded}")
    sys.meta_path.insert(0, _RefuseImports(names))


def _limit_resources(memory_mb: int) -> None:
    try:
        import resource
    except ImportError:  # Windows: no rlimits, the pool's timeouts still apply.
        return
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if memory_mb > 0:
        limit = memory_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ValueError, OSError):
            # macOS does not enforce an address-space cap; the pool's timeout
            # and the cache bound are what hold a worker there.
            pass


def _read_exact(stream: BinaryIO, n: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = n
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _u32(stream: BinaryIO) -> int | None:
    raw = _read_exact(stream, 4)
    return None if raw is None else int.from_bytes(raw, "big")


def read_request(stream: BinaryIO) -> Frame | None:
    """The next frame on ``stream``; ``None`` at a clean end of input. A frame
    that breaks the format raises :class:`FrameError`: the stream cannot be
    resynchronised, so the worker exits and the pool starts another."""
    header_length = _u32(stream)
    if header_length is None:
        return None
    if header_length > MAX_HEADER_BYTES:
        raise FrameError("header over the cap")
    raw = _read_exact(stream, header_length)
    if raw is None:
        raise FrameError("truncated header")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError("header is not JSON") from exc
    if not isinstance(header, dict):
        raise FrameError("header is not an object")
    count = _u32(stream)
    if count is None or count > MAX_BLOBS:
        raise FrameError("bad blob count")
    blobs: list[bytes] = []
    total = 0
    for _ in range(count):
        length = _u32(stream)
        if length is None:
            raise FrameError("truncated blob length")
        total += length
        if total > MAX_BLOB_BYTES:
            raise FrameError("blobs over the cap")
        blob = _read_exact(stream, length)
        if blob is None:
            raise FrameError("truncated blob")
        blobs.append(blob)
    return Frame(header=header, blobs=tuple(blobs))


def _int(header: dict[str, Any], name: str, *, minimum: int = 0) -> int:
    value = header.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise core.SandboxError("bad_request", f"{name} must be an integer >= {minimum}")
    return value


def _str(header: dict[str, Any], name: str) -> str:
    value = header.get(name)
    if not isinstance(value, str) or not value:
        raise core.SandboxError("bad_request", f"{name} must be a non-empty string")
    return value


def _peers(header: dict[str, Any]) -> frozenset[int]:
    value = header.get("peers")
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_PEERS
        or not all(
            isinstance(p, int) and not isinstance(p, bool) and p > core.SERVER_PEER_MAX
            for p in value
        )
    ):
        raise core.SandboxError("bad_request", "peers must be client Loro peer ids")
    return frozenset(value)


def _utf8(blob: bytes) -> str:
    """Text carried as a blob, UTF-8. Text never rides the header, which is
    capped far below the size of a file."""
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise core.SandboxError("bad_request", "text travels as UTF-8") from exc


def _need(cache: core.DocCache, key: str) -> Frame:
    """Ask for the document, saying where the cache stands on it (if at all)
    so the backend can send only the log after that."""
    position = cache.position(key)
    return Frame({"ok": True, "need": True, "at": list(position) if position else None})


def handle(frame: Frame, cache: core.DocCache, warmed: dict[str, str] | None = None) -> Frame:
    """One response for one request. Never raises for a bad request: every
    refusal is a response the backend reads. ``warmed`` is the start-up
    warm-up's report, which a ping carries."""
    header = frame.header
    blobs = frame.blobs
    op = header.get("op")
    try:
        if op == "ping":
            return Frame(
                {"ok": True, "loro": core.LORO_VERSION, "pid": os.getpid(), "warm": warmed or {}}
            )
        if op == "stats":
            return Frame({"ok": True, "docs": len(cache), "bytes": cache.total_bytes()})
        if op == "evict":
            cache.drop(_str(header, "key"))
            return Frame({"ok": True})
        if op == "ephemeral":
            if len(blobs) != 1:
                raise core.SandboxError("bad_request", "ephemeral carries one blob")
            judged = core.caret(
                rules=core.rules_for(header.get("rules")),
                peer=_int(header, "peer", minimum=core.SERVER_PEER_MAX + 1),
                data=blobs[0],
            )
            # A type with something to say about a caret (a notebook: the
            # cell it is in) says it beside the bytes; others answer as ever.
            reply: dict[str, Any] = {"ok": True}
            if judged.facts:
                reply["caret"] = judged.facts
            return Frame(reply, (judged.encoded,))

        key = _str(header, "key")
        epoch = _int(header, "epoch", minimum=1)
        if op == "seed":
            # One blob: the text. Two: the text, then the source's content it
            # was edited from (see ``core.seed``).
            if len(blobs) not in (1, 2):
                raise core.SandboxError("bad_request", "seed carries its text, and maybe a base")
            seeded = core.seed(
                cache,
                key=key,
                epoch=epoch,
                rules=core.rules_for(header.get("rules")),
                text=_utf8(blobs[0]),
                peer=_int(header, "peer", minimum=core.SERVER_PEER_MAX + 1),
                base=_utf8(blobs[1]) if len(blobs) == 2 else None,
                blank=header.get("blank") is True,
            )
            return Frame(
                {"ok": True, "loro": core.LORO_VERSION},
                (seeded.snapshot, seeded.vv, encode_projection(seeded.projection), seeded.base_vv),
            )

        if op == "salvage":
            # The snapshot, then each logged update: rebuilt as far as it
            # goes, never cached (see ``core.salvage``).
            if not blobs:
                raise core.SandboxError("bad_request", "salvage carries a snapshot")
            salvaged = core.salvage(
                rules=core.rules_for(header.get("rules")),
                snapshot=blobs[0],
                updates=list(blobs[1:]),
            )
            return Frame(
                {"ok": True, "applied": salvaged.applied, "skipped": salvaged.skipped},
                (salvaged.text.encode("utf-8"),),
            )

        log_seq = _int(header, "log_seq")
        if op == "load":
            if len(blobs) < 2:
                raise core.SandboxError("bad_request", "load carries a snapshot and a vector")
            loaded = core.load(
                cache,
                key=key,
                epoch=epoch,
                log_seq=log_seq,
                rules=core.rules_for(header.get("rules")),
                snapshot=blobs[0],
                updates=list(blobs[2:]),
                expect_vv=blobs[1] if header.get("expect_vv") is True else None,
            )
            return Frame({"ok": True}, (loaded.vv, encode_projection(loaded.projection)))
        if op == "validate":
            if len(blobs) != 1:
                raise core.SandboxError("bad_request", "validate carries one update")
            verdict = core.validate(
                cache,
                key=key,
                epoch=epoch,
                log_seq=log_seq,
                rules=core.rules_for(header.get("rules")),
                peers=_peers(header),
                update=blobs[0],
            )
            if verdict is None:
                return _need(cache, key)
            answer: dict[str, Any] = {
                "ok": True,
                "outcome": verdict.outcome,
                "reason": verdict.reason,
                "ops": verdict.ops,
            }
            if verdict.notes:
                answer["notes"] = verdict.notes
            return Frame(answer, (verdict.delta, verdict.vv, encode_projection(verdict.projection)))
        if op == "advance":
            if len(blobs) != 1:
                raise core.SandboxError("bad_request", "advance carries one delta")
            moved = core.advance(cache, key=key, epoch=epoch, log_seq=log_seq, delta=blobs[0])
            return Frame({"ok": True, "advanced": moved})
        if op == "catchup":
            # ``log_seq`` is where the cache stands; the blobs are the logged
            # deltas after it, in order. Stops (and drops the document) at the
            # first one it cannot follow.
            for offset, delta in enumerate(blobs, start=1):
                if not core.advance(
                    cache, key=key, epoch=epoch, log_seq=log_seq + offset, delta=delta
                ):
                    return Frame({"ok": True, "advanced": False})
            return Frame({"ok": True, "advanced": True})
        if op == "merge":
            if len(blobs) != 2:
                raise core.SandboxError("bad_request", "merge carries a base vector and the text")
            merged = core.merge(
                cache,
                key=key,
                epoch=epoch,
                log_seq=log_seq,
                rules=core.rules_for(header.get("rules")),
                peer=_int(header, "peer", minimum=core.SERVER_PEER_MAX + 1),
                base_vv=blobs[0],
                text=_utf8(blobs[1]),
                keep=header.get("keep") is True,
            )
            if merged is None:
                return _need(cache, key)
            return Frame(
                {"ok": True, "outcome": merged.outcome, "reason": merged.reason},
                (
                    merged.delta,
                    merged.vv,
                    encode_projection(merged.projection),
                    merged.base_vv,
                    merged.at_text,
                ),
            )
        if op == "content":
            if len(blobs) > 1:
                raise core.SandboxError("bad_request", "content carries at most a version")
            text = core.content(
                cache,
                key=key,
                epoch=epoch,
                log_seq=log_seq,
                rules=core.rules_for(header.get("rules")),
                at=blobs[0] if blobs else None,
            )
            if text is None:
                return _need(cache, key)
            return Frame({"ok": True}, (text,))
        if op == "latest":
            found = core.latest(
                cache,
                key=key,
                epoch=epoch,
                log_seq=log_seq,
                rules=core.rules_for(header.get("rules")),
            )
            if found is None:
                return _need(cache, key)
            return Frame({"ok": True}, found)
        if op in _NOTEBOOK_OPS:
            return _notebook(op, header, blobs, cache, key=key, epoch=epoch, log_seq=log_seq)
        if op in ("export", "snapshot"):
            if op == "export":
                since = blobs[0] if header.get("since") is True and blobs else None
                exported = core.export(cache, key=key, epoch=epoch, log_seq=log_seq, since=since)
            else:
                exported = core.snapshot(
                    cache,
                    key=key,
                    epoch=epoch,
                    log_seq=log_seq,
                    expect_vv=blobs[0] if header.get("expect_vv") is True and blobs else None,
                )
            if exported is None:
                return _need(cache, key)
            return Frame({"ok": True, "mode": exported.mode}, (exported.data, exported.vv))
        raise core.SandboxError("bad_request", f"unknown op {op!r}")
    except core.SandboxError as exc:
        return Frame({"ok": False, "code": exc.code, "message": exc.message[:500]})


_NOTEBOOK_OPS = frozenset(
    {"nb_ops", "nb_rebase_ops", "nb_rebase_update", "nb_view", "nb_normalize", "nb_graph"}
)


def _json_blob(blob: bytes) -> Any:
    try:
        return json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise core.SandboxError("bad_request", "a JSON blob that does not read") from exc


def _applied(applied: notebook_ops.OpsApplied) -> Frame:
    return Frame(
        {"ok": True, "outcome": applied.outcome},
        (
            applied.delta,
            applied.vv,
            encode_projection(applied.projection),
            encode_projection(applied.result),
        ),
    )


def _notebook(
    op: str,
    header: dict[str, Any],
    blobs: tuple[bytes, ...],
    cache: core.DocCache,
    *,
    key: str,
    epoch: int,
    log_seq: int,
) -> Frame:
    """The notebook type's own requests (see :mod:`.notebook`)."""
    if op == "nb_ops":
        # Blobs: the operations (JSON), then the base vector (none: the head).
        if len(blobs) not in (1, 2):
            raise core.SandboxError("bad_request", "nb_ops carries its operations and a base")
        ops = _json_blob(blobs[0])
        if not isinstance(ops, list):
            raise core.SandboxError("bad_request", "the operations are a list")
        try:
            applied = notebook_ops.apply_ops(
                cache,
                key=key,
                epoch=epoch,
                log_seq=log_seq,
                peer=_int(header, "peer", minimum=core.SERVER_PEER_MAX + 1),
                base_vv=blobs[1] if len(blobs) == 2 else None,
                ops=ops,
            )
        except notebook_ops.OpError as exc:
            return Frame(
                {
                    "ok": True,
                    "outcome": "op_error",
                    "op_index": exc.index,
                    "code": exc.code,
                    "message": exc.message[:500],
                }
            )
        return _need(cache, key) if applied is None else _applied(applied)
    if op == "nb_rebase_ops":
        # Blobs: the operations (JSON), the old epoch's last state, the
        # version in it the batch was made on, and the version of this epoch
        # whose content is that last state.
        if len(blobs) != 4:
            raise core.SandboxError("bad_request", "nb_rebase_ops carries four blobs")
        ops = _json_blob(blobs[0])
        if not isinstance(ops, list):
            raise core.SandboxError("bad_request", "the operations are a list")
        try:
            rebased = notebook_ops.rebase_ops(
                cache,
                key=key,
                epoch=epoch,
                log_seq=log_seq,
                peer=_int(header, "peer", minimum=core.SERVER_PEER_MAX + 1),
                ops=ops,
                tail_snapshot=blobs[1],
                base_vv=blobs[2],
                next_base_vv=blobs[3],
            )
        except notebook_ops.OpError as exc:
            return Frame(
                {
                    "ok": True,
                    "outcome": "op_error",
                    "op_index": exc.index,
                    "code": exc.code,
                    "message": exc.message[:500],
                }
            )
        return _need(cache, key) if rebased is None else _applied(rebased)
    if op == "nb_rebase_update":
        # Blobs: the old epoch's last state, the editor's update written on
        # it, and the version of this epoch whose content is that state.
        if len(blobs) != 3:
            raise core.SandboxError("bad_request", "nb_rebase_update carries three blobs")
        carried = notebook_ops.rebase_update(
            cache,
            key=key,
            epoch=epoch,
            log_seq=log_seq,
            peer=_int(header, "peer", minimum=core.SERVER_PEER_MAX + 1),
            tail_snapshot=blobs[0],
            update=blobs[1],
            next_base_vv=blobs[2],
        )
        return _need(cache, key) if carried is None else _applied(carried)
    if op == "nb_normalize":
        normalized = notebook_ops.normalize_cached(
            cache,
            key=key,
            epoch=epoch,
            log_seq=log_seq,
            peer=_int(header, "peer", minimum=core.SERVER_PEER_MAX + 1),
        )
        return _need(cache, key) if normalized is None else _applied(normalized)
    if op == "nb_view":
        found = notebook_ops.view(
            cache,
            key=key,
            epoch=epoch,
            log_seq=log_seq,
            frontier=blobs[0] if blobs else None,
        )
        if found is None:
            return _need(cache, key)
        view, vv = found
        return Frame({"ok": True}, (encode_projection(view), vv))
    graph = notebook_ops.graph(cache, key=key, epoch=epoch, log_seq=log_seq)
    if graph is None:
        return _need(cache, key)
    analysed, vv = graph
    return Frame({"ok": True}, (encode_projection(analysed), vv))


def serve(
    stdin: BinaryIO, stdout: BinaryIO, cache: core.DocCache, warmed: dict[str, str] | None = None
) -> None:
    while True:
        request = read_request(stdin)
        if request is None:
            return
        reply = handle(request, cache, warmed)
        # The request's id goes back on its reply, so the pool can tell an
        # answer to this request from one to a request it gave up on.
        if "id" in request.header:
            reply.header["id"] = request.header["id"]
        stdout.write(encode_frame(reply))
        stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crdt-sandbox-worker")
    parser.add_argument("--memory-mb", type=int, default=1024)
    parser.add_argument("--cache-docs", type=int, default=256)
    parser.add_argument("--cache-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args(argv)
    _refuse_imports(REFUSED_IMPORTS)
    _limit_resources(args.memory_mb)
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    # Frames are the only thing on stdout; anything else anyone prints goes
    # to stderr, where the pool logs it.
    sys.stdout = sys.stderr
    cache = core.DocCache(max_docs=args.cache_docs, max_bytes=args.cache_bytes)
    # Under the same caps as every request, and before the first frame is
    # read: the pool's first ping waits for it, and no request's budget does.
    warmed = core.warm_all()
    for name, outcome in warmed.items():
        if outcome.startswith("failed"):
            print(f"crdt sandbox: warming {name} {outcome}", file=sys.stderr)
    try:
        serve(stdin, stdout, cache, warmed)
    except FrameError as exc:
        print(f"crdt sandbox: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
