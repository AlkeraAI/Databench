// == ALKERA EDIT START — whole file (alkera-local; see vendor/opencode/README.alkera.md).
import { describe, expect } from "bun:test"
import { NodeFileSystem } from "@effect/platform-node"
import { AppFileSystem } from "@opencode-ai/core/filesystem"
import { Effect, Layer } from "effect"
import { Truncate } from "@/tool/truncate"
import { testEffect } from "../lib/effect"

const it = testEffect(Layer.mergeAll(Truncate.defaultLayer, NodeFileSystem.layer, AppFileSystem.defaultLayer))

/** A text the ordinary truncator always shortens, so a result that comes back
 *  unchanged can only have skipped it. */
const OVERSIZED = Array.from({ length: Truncate.MAX_LINES + 500 }, (_, i) => `line${i}`).join("\n")

const HOST_FILE = "/tmp/alkera-host-spill/bash-deadbeef.txt"

/** An MCP result carrying whatever `_meta` the host stamped on it. */
const withMeta = (meta: unknown) => ({ content: [{ type: "text", text: "..." }], _meta: meta })

const spilled = (outputPath: string) => withMeta({ [Truncate.HOST_SPILL_META_KEY]: { outputPath } })

/** The truncation directory's current entries — the file a re-spill would add. */
const entries = Effect.gen(function* () {
  const fsys = yield* AppFileSystem.Service
  return yield* fsys.readDirectory(Truncate.DIR).pipe(Effect.catch(() => Effect.succeed([] as string[])))
})

describe("Truncate.hostSpill", () => {
  it.live("reads the path the host spilled the whole output to", () =>
    Effect.sync(() => {
      expect(Truncate.hostSpill(spilled(HOST_FILE))).toBe(HOST_FILE)
    }),
  )

  it.live("has no path for a result the host did not shorten", () =>
    Effect.sync(() => {
      for (const result of [
        undefined,
        null,
        "a string",
        42,
        { content: [] },
        withMeta(undefined),
        withMeta(null),
        withMeta("not an object"),
        withMeta({}),
        withMeta({ "some.other/key": { outputPath: HOST_FILE } }),
        withMeta({ [Truncate.HOST_SPILL_META_KEY]: null }),
        withMeta({ [Truncate.HOST_SPILL_META_KEY]: "a string" }),
        withMeta({ [Truncate.HOST_SPILL_META_KEY]: {} }),
        withMeta({ [Truncate.HOST_SPILL_META_KEY]: { outputPath: "" } }),
        withMeta({ [Truncate.HOST_SPILL_META_KEY]: { outputPath: 7 } }),
      ]) {
        expect(Truncate.hostSpill(result)).toBeUndefined()
      }
    }),
  )
})

describe("Truncate.toolOutput", () => {
  it.live("keeps the host's pointer and writes no file of its own", () =>
    Effect.gen(function* () {
      const svc = yield* Truncate.Service
      const before = yield* entries

      const result = yield* Truncate.toolOutput(svc, OVERSIZED, spilled(HOST_FILE))

      expect(result.truncated).toBe(true)
      if (!result.truncated) throw new Error("expected truncated")
      // The pointer names the host's file, not one of ours: a reader following it
      // reaches the whole output instead of a copy of the preview.
      expect(result.outputPath).toBe(HOST_FILE)
      // The host already bounded this text, so it reaches the model as it stands —
      // no second preview, no second "Full output saved to" naming our file.
      expect(result.content).toBe(OVERSIZED)
      expect(result.content).not.toContain("truncated...")

      const after = yield* entries
      expect(after.filter((name) => !before.includes(name))).toEqual([])
    }),
  )

  it.live("keeps the host's pointer even when the shortened text fits", () =>
    Effect.gen(function* () {
      const svc = yield* Truncate.Service

      const result = yield* Truncate.toolOutput(svc, "a short tail", spilled(HOST_FILE))

      // The host cut this result; that the tail it handed back is small does not
      // make the result whole, so the row still says where the rest went.
      expect(result.truncated).toBe(true)
      if (!result.truncated) throw new Error("expected truncated")
      expect(result.outputPath).toBe(HOST_FILE)
      expect(result.content).toBe("a short tail")
    }),
  )

  it.live("still shortens a result the host did not shorten", () =>
    Effect.gen(function* () {
      const svc = yield* Truncate.Service

      const result = yield* Truncate.toolOutput(svc, OVERSIZED, { content: [] })

      expect(result.truncated).toBe(true)
      if (!result.truncated) throw new Error("expected truncated")
      expect(result.content).toContain("truncated...")
      expect(result.outputPath).toContain("tool_")

      const fsys = yield* AppFileSystem.Service
      expect(yield* fsys.readFileString(result.outputPath)).toBe(OVERSIZED)
    }),
  )

  it.live("falls back to shortening when the marker names no file", () =>
    Effect.gen(function* () {
      const svc = yield* Truncate.Service

      // A marker with nothing to point at must not turn the bound off: keeping a
      // path that does not exist would be worse than shortening again.
      const result = yield* Truncate.toolOutput(svc, OVERSIZED, withMeta({ [Truncate.HOST_SPILL_META_KEY]: {} }))

      expect(result.truncated).toBe(true)
      if (!result.truncated) throw new Error("expected truncated")
      expect(result.outputPath).toContain("tool_")
    }),
  )

  it.live("leaves a result that fits alone", () =>
    Effect.gen(function* () {
      const svc = yield* Truncate.Service

      const result = yield* Truncate.toolOutput(svc, "line1\nline2", { content: [] })

      expect(result.truncated).toBe(false)
      expect(result.content).toBe("line1\nline2")
    }),
  )
})
// == ALKERA EDIT END
