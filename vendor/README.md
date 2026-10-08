# `vendor/`

Third-party source we keep in-tree.

## `vendor/opencode/` — opencode subtree

[Opencode](https://github.com/sst/opencode) is the harness we wrap as
our first concrete `HarnessAdapter` implementation. It's a **git
subtree** (NOT a submodule — there is no `.gitmodules`): the source is
committed directly into this repo via squash merges, and built into a
standalone binary that the daemon bundles via
`scripts/build-opencode-binary.sh`. See
[`vendor/opencode/README.alkera.md`](opencode/README.alkera.md) for the
subtree contract.

### One-time setup

Nothing. A plain `git clone` of `origin/main` already contains
`vendor/opencode/` — it's committed in-tree, so there is **no submodule
to init**.

### Build the binary

```bash
make opencode-binary    # → apps/cli/dist/opencode/opencode (staged; opencode.exe on Windows)
```

Requires [`bun`](https://bun.sh/docs/installation) on PATH. The build
is idempotent: re-running with the same subtree SHA is a no-op (the
"version" is the most recent commit on this repo that touched
`vendor/opencode/`).

### Bump to a newer upstream commit

```bash
make opencode-fetch-upstream   # adds the upstream remote + lists new commits
make opencode-bump             # git subtree pull --prefix=vendor/opencode --squash
# Resolve any conflicts with our local patches, test, then:
git add vendor/opencode
git commit -m "chore(opencode): bump to <short-sha>"
```

Test before committing — opencode's HTTP API or event shape may shift
across upstream commits. Run `make e2e` (the bun-driven opencode e2e
suite) to validate.

### Do NOT edit opencode casually

Treat opencode as opaque upstream source. The few changes we do carry
(e.g. the Windows smoke-test fix in `packages/opencode/script/build.ts`)
live as **normal commits** touching `vendor/opencode/` — list them with
`make opencode-show-patches`. `git subtree pull --squash` replays our
history on top of upstream, so these patches re-apply on each bump
(resolve conflicts if upstream moved the same lines). Keep such patches
minimal and consider upstreaming them.

### Why a subtree (not a submodule)

- The source is vendored into our history — every contributor and CI
  checkout gets the exact same opencode with no extra fetch step, and
  no broken builds from an un-inited submodule.
- We can carry small local patches as ordinary commits (above), which a
  read-only submodule pin can't express cleanly.
- Upstream sync is an explicit, reviewable `git subtree pull --squash`
  (`make opencode-bump`), not an implicit floating pointer.
