# `alkera_core.project`

Manages each workspace's `.alkera/` directory, the per-project state the CLI and the daemon share.

## Layout

```
<workspace>/.alkera/
├── chats/
│   └── <session_id>/
│       ├── chat.jsonl       # append-only event log, the source of truth
│       ├── manifest.json    # session metadata cache (atomic rewrites)
│       └── .lock            # per-chat write lock (PID-stamped JSON)
├── blobs/
│   └── <ab>/<cd>/<sha256>   # content-addressed blob store
└── blobs.gc.lock            # advisory lock held during a blob sweep
```

The models these files hold, and how they evolve, are in [`../versioning/README.md`](../versioning/README.md).

## Entry point

```python
from alkera_core.project import ProjectDirectory

# Creates the tree if it is missing; does nothing if it exists.
project = ProjectDirectory(workspace_root / ".alkera")

# Sub-manager handles. Cheap to construct, with no shared state.
chats = project.chats()
blobs = project.blobs()
```

Never build `.alkera/` paths by hand. `ProjectDirectory` owns the layout, and a new area of the directory gets a new method on it.

## Chats

```python
# Create a chat. Returns a locked Chat handle.
with chats.create(title="Investigate slow query") as chat:
    chat.append_event(some_event)
    # leaving the block releases the lock and writes the manifest

# Reopen it later.
with chats.open(session_id) as chat:
    for event in chat.events():
        ...

# Async variants, used by the daemon.
async with await chats.open_async(session_id) as chat:
    await chat.append_event(some_event)
    async for event in chat.events():
        ...
```

`Chat` and `AsyncChat` (which runs the same calls through `asyncio.to_thread`) hold the same lock. Use whichever fits the call site.

## The per-chat write lock

Opening a chat takes `<chat>/.lock`, a JSON file recording the PID, host, time and a per-acquisition nonce.

- **One client mutates a chat at a time.** A second attempt raises `LockHeldError(holder=...)`; the caller decides whether to retry.
- **A stale lock is reclaimed.** When the recorded PID is dead on the same host, the next acquirer takes the lock and keeps the old file beside it as `.lock.stale.<unix_ts>.<rand>`. A holder on another host is treated as live, since it cannot be probed. Liveness goes through `alkera_core.process.process_alive`, which works on POSIX and Windows.
- **Release, from best case to worst:**
  1. `chat.close()` or leaving the `with` block deletes the lock.
  2. A clean process exit releases every held lock through `atexit`.
  3. SIGTERM and SIGINT handlers release held locks, then chain to the previous handler.
  4. `weakref.finalize` releases a lock the caller forgot.
  5. SIGKILL or power loss leaves the file behind. The next acquirer sees the dead PID and reclaims it.

The lock file is published atomically: `os.link` on POSIX and `os.rename` on Windows, both of which fail when the lock already exists.

## Atomic writes

`alkera_core.atomic_io`:

- `write_text_atomic(path, text)` writes to a temporary file beside `path`, fsyncs it, and moves it into place with `os.replace`. Readers see the old file or the new one, never a partial one.
- `write_json_atomic(path, payload)` encodes JSON and writes it atomically.
- `append_line(path, line)` appends and fsyncs. Used for JSONL; the caller must hold the chat lock.

## Crash-safe JSONL

`alkera_core.project.jsonl.iter_jsonl(path)` streams events:

- A missing file yields nothing.
- A truncated last line (no `\n`), left by a writer that crashed mid-line, is skipped.
- A malformed line in the middle of the file is skipped.
- A line that is valid JSON but not an object is skipped.

## Blob store

```python
sha, size = blobs.write(b"some bytes")          # idempotent
data = blobs.read(sha)
report = chats.gc(grace_period_seconds=86_400)  # mark and sweep
```

- Content-addressed at `blobs/<ab>/<cd>/<sha256>`.
- Writes are atomic and skipped when the blob already exists.
- Garbage collection is mark and sweep over the chat logs, not reference counting.
- A grace period (24 hours by default) protects a blob written but not yet referenced from any `chat.jsonl`.
- `blobs.gc.lock` keeps two sweeps from running at once.

## Tests

```bash
uv run pytest packages/api-core/tests/project/
```

- `packages/api-core/tests/project/test_locking.py` covers the lock in one process.
- `packages/api-core/tests/project/chats/test_concurrent_access.py` runs real subprocesses: `atexit`, SIGTERM, SIGKILL followed by a stale reclaim, a race for the lock, and a handoff.
- `packages/api-core/tests/project/chats/test_async_chat.py` holds `AsyncChat` to the same behaviour as `Chat`.
- `packages/api-core/tests/project/chats/test_blobs.py` covers the blob store and its garbage collection.
