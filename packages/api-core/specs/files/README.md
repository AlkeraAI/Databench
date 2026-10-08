# Files TLA+ specifications

Two Files protocols are model-checked here as well as tested. Integration tests
cannot cover them well, because the bad interleaving is rare and its consequence
is silent data loss.

| Spec | Protocol | Invariants |
| --- | --- | --- |
| `commit_gc.tla` | a content commit racing the garbage sweep | `NoDanglingReference`: no `ready` version ever references an object the sweep moved |
| `lease_fencing.tla` | folder lease acquire / heartbeat / reap, with epoch fencing | `SingleWriter`, `EpochMonotonic`, `NoStaleWrite`; liveness `EventuallyGrantable` |

`_smoke.tla` is a five-state counter. It proves the pipeline (fetch the jar, pick a
runtime, run TLC, report a verdict) works, and gives the tooling test a spec it can
deliberately break.

## Running them

```bash
make files-specs                       # every *.tla here that has a sibling *.cfg
ops/scripts/tlc.sh packages/api-core/specs/files/_smoke.tla \
                   packages/api-core/specs/files/_smoke.cfg 4     # one spec, 4 workers
```

`make files-specs` first runs `ops/scripts/ensure-tla-tools.sh`, which installs the
pinned `tla2tools.jar` into `.cache/tla/` (gitignored) and verifies its SHA-256 on
every run. A jar that does not match the pin is deleted, never executed.

Those bytes come from a **jar committed to this repo** (`ops/tools/tla/`), not from the
network. Upstream's `v1.8.0` is a rolling pre-release: its CI re-cuts the tag and
re-uploads `tla2tools.jar`, so the asset changes while the version does not and any byte
pin against it breaks on the next upstream build. The last non-rolling release, `v1.7.4`,
ships TLC 2.19, which cannot drive these specs: it writes no `\* <action(...) ... of
module ...>` header above each state of a `-simulate` dump, and the trace-conformance
replays read the action name out of exactly that header. So the build the specs were
developed against is vendored and still SHA-verified on every run; a version with no
vendored copy is still downloaded, so a future immutable release is a two-line change to
the tables in that script.

`ops/scripts/tlc.sh` runs TLC under a real `java` when the host has a working one and
under `eclipse-temurin:21-jre` in Docker otherwise. macOS ships a `/usr/bin/java` stub that exists on `PATH` but has no runtime behind it,
so the probe is `java -version`, not `command -v java`. The exit code is TLC's, so a
violated invariant fails the target. TLC's metadata goes to `.cache/tla/meta/<spec>/`
so a failing run leaves this directory byte-identical.

A Docker run never outlives the script. Its container is removed when the script exits,
is interrupted or is terminated, and the run stops with exit 124 after
`TLC_WALL_SECONDS` (default 3600). A container left by a script that was killed outright
stops itself 30 seconds after that bound, and the next run removes any whose script is
gone. `TLC_RUNTIME=docker` or `TLC_RUNTIME=java` picks the runtime instead of probing.
A Docker that does not answer `docker info` in 15 seconds is exit 2.

## Checkpoint names and spec actions

A spec that is checked but not connected to the code proves nothing about the code.
Every spec here is bound to the implementation through
`alkera_core/files/checkpoints.py`: each `await cp.reach("<name>")` in the
implementation is one action in the spec, spelled the same way.

The convention every spec in this directory follows:

1. **One TLA+ action per checkpoint, same name, `snake_case` on both sides.** A spec
   action `commit_verified` is reached in the implementation as
   `await cp.reach("commit_verified")`, at the point *between* the two effects the
   action orders (after the store `put` has landed, before the row `COMMIT`).
2. **Actions are named for the effect that just became visible**, not for the function
   that ran: `object_put`, `sweep_scanned`, `reference_invalidated`, `object_moved`,
   `lease_acquired`, `heartbeat_rejected`. A reviewer reading the spec and a reviewer
   reading the code must be able to line them up without a translation table.
3. **The spec declares the checkpoint set it owns** in a comment block at the top of
   the module, so a checkpoint added to the code with no action in the spec is a
   review-visible gap rather than an invisible one.
4. **Trace conformance replays a TLC counterexample or behaviour through the
   implementation** by releasing exactly those checkpoints in the order the trace lists
   them (`packages/api-core/tests/files/`, the conformance test for `commit_gc`). A
   trace step whose name has no checkpoint fails the replay, which keeps the two from
   drifting apart.
5. **Checkpoints are a no-op in production.** They exist so tests can force an exact
   interleaving against real Postgres; nothing outside a test ever pauses on one.

Adding a spec: drop `<name>.tla` and `<name>.cfg` here and `make files-specs` picks it
up. There is no list to edit.

### `lease_fencing.tla` actions and checkpoints

The spec's own header carries this table too, so a reader of either artefact can line
them up. `leases.*` names are the strings passed to `cp.reach(...)` in
`packages/api-core/alkera_core/files/leases.py`; the last four actions are the ones the
implementation does **not** yet pause at, and the trace replay drives them through the
call named instead.

| Spec action | Implementation | Checkpoint today |
| --- | --- | --- |
| `leases_overlap_checked` | `LeaseService.acquire`, between the covering-lease read and the grant | `leases.overlap_checked` |
| `leases_acquired` | the `INSERT … ON CONFLICT DO UPDATE … WHERE` that grants the epoch | `leases.acquired` |
| `leases_final_applied` | `LeaseService.release`, after the final snapshot, before the commit | `leases.final_applied` |
| `leases_heartbeat` | `LeaseService.heartbeat`, `UPDATE … WHERE epoch = $e AND holder_instance_id = $i` | none (driven by the call) |
| `leases_reaped` | the reaper's lapse statement (`released_at`, `hwm`, `grantable_after`) | none |
| `leases_fenced_write` | `assert_lease_epoch` / `fenced_write`, the `FOR SHARE` re-read | none |
| `platform_restored` | a restore bumping `file_platform.restore_generation` | none |
| `clock_tick`, `holder_paused`, `holder_resumed` | the injected `Clock`; a holder that stops beating | none (environment) |

The four missing checkpoints are a known, visible gap (convention 3 above):
adding `cp.reach("leases.heartbeat" / ".reaped" / ".fenced_write")` to the
implementation is what would let the replay pause *inside* those statements rather than
around them.

`EventuallyGrantable` needs the clock to keep moving, which a bounded model cannot
offer, so expiry saturates at `MaxClock`: at the bound every lease reads expired and
the reaper is enabled, which is the behaviour real time would produce. The same bound
makes the terminal states unreachable-by-any-action, so the config sets
`CHECK_DEADLOCK FALSE`; the liveness property, not the deadlock check, is what proves a
holder cannot keep a folder hostage.

### `commit_gc.tla` actions and checkpoints

The spec's own header carries this table too. `content.*` names are the strings passed
to `cp.reach(...)` in `packages/api-core/alkera_core/files/content.py`, `gc.*` those in
`gc.py`. Everything below the rule is an action the implementation does **not** pause
at, a visible gap rather than a silent one.

| Spec action | Implementation | Checkpoint today |
| --- | --- | --- |
| `content_after_store_put` | after `ObjectStore.put` landed, before any row names the key | `content.after_store_put` |
| `content_before_commit` | `ContentService._commit`, after the re-hash, before the version row COMMITs | `content.before_commit` |
| `gc_after_reachability` | `Janitor.sweep`, after the roots query and the once-only stamp | `gc.after_reachability` |
| `gc_before_move` | after the candidate is confirmed, before the store move, the guard | `gc.before_move` |
| `gc_after_move` | the bytes are under `deleted/` | `gc.after_move` |
| `content_committing` | the session's row moves to `committing`; still a root | none (read off `file_upload_sessions.state`) |
| `reference_invalidated` | the trash purge dropping a version's reference | none |
| `object_restored` / `object_expired` | `Janitor` restore, and expiry past the window | none |
| `session_crashed` / `sweep_crashed` | an abandoned upload; the sweeper killed between the two move checkpoints | none |
| `grant_opened`, `grant_closed`, `clock_tick` | an in-flight download grant; the injected `Clock` | none (environment) |

Conformance runs the opposite way to `lease_fencing`'s. There, TLC simulates behaviours
and the implementation is driven through them. A commit racing the sweep cannot be
driven that way (which behaviour happens is decided by where the code parks), so
`packages/api-core/tests/files/specs/test_files_commit_gc_trace.py` runs the race for real through the
checkpoints above, records each step as one of the actions in this table with the state
read back from Postgres and the store, and generates a *trace module* that extends
`commit_gc` and pins state `i+1` to the recorded one while still requiring `Next` to
justify the step. A recorded step the protocol cannot take leaves TLC with no successor,
which under `CHECK_DEADLOCK TRUE` is the InvalidTrace report naming the index.

The trace module declares `ob1, ob2, s1, s2, nobody` as constants assigned to themselves
in the generated config: TLC parses a module before it reads the configuration, so a
bare model value inside the recorded states would otherwise be an unknown operator.
