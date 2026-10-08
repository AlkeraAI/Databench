import { describe, expect, test } from "bun:test"
import path from "path"
import { Effect, FileSystem, Layer } from "effect"
import { FetchHttpClient } from "effect/unstable/http"
import { NodeFileSystem } from "@effect/platform-node"
import { CrossSpawnSpawner } from "@opencode-ai/core/cross-spawn-spawner"
import { AppFileSystem } from "@opencode-ai/core/filesystem"
import { ModelID, ProviderID } from "../../src/provider/schema"
import { Instruction } from "../../src/session/instruction"
import type { MessageV2 } from "../../src/session/message-v2"
import { MessageID, PartID, SessionID } from "../../src/session/schema"
import { Global } from "@opencode-ai/core/global"
import { RuntimeFlags } from "../../src/effect/runtime-flags"
import { provideInstance, provideTmpdirInstance, tmpdirScoped } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import { TestConfig } from "../fixture/config"

const it = testEffect(Layer.mergeAll(CrossSpawnSpawner.defaultLayer, NodeFileSystem.layer))

const configLayer = TestConfig.layer()

const instructionLayer = (global: Partial<Global.Interface>, flags: Partial<RuntimeFlags.Info> = {}) =>
  Instruction.layer.pipe(
    Layer.provide(configLayer),
    Layer.provide(AppFileSystem.defaultLayer),
    Layer.provide(FetchHttpClient.layer),
    Layer.provide(Global.layerWith(global)),
    Layer.provide(RuntimeFlags.layer(flags)),
  )

const provideInstruction =
  (global: Partial<Global.Interface>, flags?: Partial<RuntimeFlags.Info>) =>
  <A, E, R>(self: Effect.Effect<A, E, R>) =>
    self.pipe(Effect.provide(instructionLayer(global, flags)))

const write = (filepath: string, content: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem
    yield* fs.makeDirectory(path.dirname(filepath), { recursive: true })
    yield* fs.writeFileString(filepath, content)
  })

const writeFiles = (dir: string, files: Record<string, string>) =>
  Effect.all(
    Object.entries(files).map(([file, content]) => write(path.join(dir, file), content)),
    { discard: true },
  )

const withFiles = <A, E, R>(files: Record<string, string>, self: (dir: string) => Effect.Effect<A, E, R>) =>
  provideTmpdirInstance((dir) =>
    Effect.gen(function* () {
      yield* writeFiles(dir, files)
      return yield* self(dir).pipe(provideInstruction({ home: dir, config: dir }))
    }),
  )

const tmpWithFiles = (files: Record<string, string>) =>
  Effect.gen(function* () {
    const dir = yield* tmpdirScoped()
    yield* writeFiles(dir, files)
    return dir
  })

function loaded(filepath: string): MessageV2.WithParts[] {
  const sessionID = SessionID.make("session-loaded-1")
  const messageID = MessageID.make("msg_message-loaded-1")

  return [
    {
      info: {
        id: messageID,
        sessionID,
        role: "user",
        time: { created: 0 },
        agent: "build",
        model: {
          providerID: ProviderID.make("anthropic"),
          modelID: ModelID.make("claude-sonnet-4-20250514"),
        },
      },
      parts: [
        {
          id: PartID.make("prt_part-loaded-1"),
          messageID,
          sessionID,
          type: "tool",
          callID: "call-loaded-1",
          tool: "read",
          state: {
            status: "completed",
            input: {},
            output: "done",
            title: "Read",
            metadata: { loaded: [filepath] },
            time: { start: 0, end: 1 },
          },
        },
      ],
    },
  ]
}

describe("Instruction.resolve", () => {
  it.live("returns empty when AGENTS.md is at project root (already in systemPaths)", () =>
    withFiles({ "AGENTS.md": "# Root Instructions", "src/file.ts": "const x = 1" }, (dir) =>
      Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const system = yield* svc.systemPaths()
        expect(system.has(path.join(dir, "AGENTS.md"))).toBe(true)

        const results = yield* svc.resolve([], path.join(dir, "src", "file.ts"), MessageID.make("msg_message-test-1"))
        expect(results).toEqual([])
      }),
    ),
  )

  it.live("returns AGENTS.md from subdirectory (not in systemPaths)", () =>
    withFiles({ "subdir/AGENTS.md": "# Subdir Instructions", "subdir/nested/file.ts": "const x = 1" }, (dir) =>
      Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const system = yield* svc.systemPaths()
        expect(system.has(path.join(dir, "subdir", "AGENTS.md"))).toBe(false)

        const results = yield* svc.resolve(
          [],
          path.join(dir, "subdir", "nested", "file.ts"),
          MessageID.make("msg_message-test-2"),
        )
        expect(results.length).toBe(1)
        expect(results[0].filepath).toBe(path.join(dir, "subdir", "AGENTS.md"))
      }),
    ),
  )

  it.live("doesn't reload AGENTS.md when reading it directly", () =>
    withFiles({ "subdir/AGENTS.md": "# Subdir Instructions", "subdir/nested/file.ts": "const x = 1" }, (dir) =>
      Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const filepath = path.join(dir, "subdir", "AGENTS.md")
        const system = yield* svc.systemPaths()
        expect(system.has(filepath)).toBe(false)

        const results = yield* svc.resolve([], filepath, MessageID.make("msg_message-test-3"))
        expect(results).toEqual([])
      }),
    ),
  )

  it.live("does not reattach the same nearby instructions twice for one message", () =>
    withFiles({ "subdir/AGENTS.md": "# Subdir Instructions", "subdir/nested/file.ts": "const x = 1" }, (dir) =>
      Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const filepath = path.join(dir, "subdir", "nested", "file.ts")
        const id = MessageID.make("msg_message-claim-1")

        const first = yield* svc.resolve([], filepath, id)
        const second = yield* svc.resolve([], filepath, id)

        expect(first).toHaveLength(1)
        expect(first[0].filepath).toBe(path.join(dir, "subdir", "AGENTS.md"))
        expect(second).toEqual([])
      }),
    ),
  )

  it.live("clear allows nearby instructions to be attached again for the same message", () =>
    withFiles({ "subdir/AGENTS.md": "# Subdir Instructions", "subdir/nested/file.ts": "const x = 1" }, (dir) =>
      Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const filepath = path.join(dir, "subdir", "nested", "file.ts")
        const id = MessageID.make("msg_message-claim-2")

        const first = yield* svc.resolve([], filepath, id)
        yield* svc.clear(id)
        const second = yield* svc.resolve([], filepath, id)

        expect(first).toHaveLength(1)
        expect(second).toHaveLength(1)
        expect(second[0].filepath).toBe(path.join(dir, "subdir", "AGENTS.md"))
      }),
    ),
  )

  it.live("skips instructions already reported by prior read metadata", () =>
    withFiles({ "subdir/AGENTS.md": "# Subdir Instructions", "subdir/nested/file.ts": "const x = 1" }, (dir) =>
      Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const agents = path.join(dir, "subdir", "AGENTS.md")
        const filepath = path.join(dir, "subdir", "nested", "file.ts")
        const id = MessageID.make("msg_message-claim-3")

        const results = yield* svc.resolve(loaded(agents), filepath, id)
        expect(results).toEqual([])
      }),
    ),
  )

  test.todo("fetches remote instructions from config URLs via HttpClient", () => {})
})

describe("Instruction.system", () => {
  it.live("loads both project and global AGENTS.md when both exist", () =>
    Effect.gen(function* () {
      const globalTmp = yield* tmpWithFiles({ "AGENTS.md": "# Global Instructions" })
      const projectTmp = yield* tmpWithFiles({ "AGENTS.md": "# Project Instructions" })

      yield* Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const paths = yield* svc.systemPaths()
        expect(paths.has(path.join(projectTmp, "AGENTS.md"))).toBe(true)
        expect(paths.has(path.join(globalTmp, "AGENTS.md"))).toBe(true)

        const rules = yield* svc.system()
        expect(rules).toHaveLength(2)
        expect(rules[0]).toBe(`Instructions from: ${path.join(globalTmp, "AGENTS.md")}\n# Global Instructions`)
        expect(rules[1]).toBe(`Instructions from: ${path.join(projectTmp, "AGENTS.md")}\n# Project Instructions`)
      }).pipe(provideInstance(projectTmp), provideInstruction({ home: globalTmp, config: globalTmp }))
    }),
  )

  it.live("skips project and global CLAUDE.md when Claude Code prompt is disabled", () =>
    Effect.gen(function* () {
      const globalTmp = yield* tmpWithFiles({ ".claude/CLAUDE.md": "# Global Claude" })
      const projectTmp = yield* tmpWithFiles({ "CLAUDE.md": "# Project Claude" })

      yield* Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const paths = yield* svc.systemPaths()
        expect(paths.has(path.join(globalTmp, ".claude", "CLAUDE.md"))).toBe(false)
        expect(paths.has(path.join(projectTmp, "CLAUDE.md"))).toBe(false)
        expect(yield* svc.system()).toEqual([])
      }).pipe(
        provideInstance(projectTmp),
        provideInstruction({ home: globalTmp, config: globalTmp }, { disableClaudeCodePrompt: true }),
      )
    }),
  )
})

describe("Instruction.systemPaths global config", () => {
  it.live("uses Global.Service config AGENTS.md", () =>
    Effect.gen(function* () {
      const globalTmp = yield* tmpWithFiles({ "AGENTS.md": "# Global Instructions" })
      const projectTmp = yield* tmpdirScoped()

      yield* Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const paths = yield* svc.systemPaths()
        expect(paths.has(path.join(globalTmp, "AGENTS.md"))).toBe(true)
      }).pipe(provideInstance(projectTmp), provideInstruction({ home: globalTmp, config: globalTmp }))
    }),
  )
})

// == ALKERA EDIT START — ALKERA.md (additive + precedence) and the size caps.
describe("Instruction ALKERA.md", () => {
  it.live("loads ALKERA.md with precedence (first) and additively alongside AGENTS.md at root", () =>
    withFiles({ "ALKERA.md": "# Alkera Rules", "AGENTS.md": "# Agents Rules" }, (dir) =>
      Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const paths = yield* svc.systemPaths()
        expect(paths.has(path.join(dir, "ALKERA.md"))).toBe(true)
        expect(paths.has(path.join(dir, "AGENTS.md"))).toBe(true)

        const rules = yield* svc.system()
        // ALKERA.md leads (precedence); AGENTS.md follows (additive, not suppressed).
        expect(rules).toHaveLength(2)
        expect(rules[0]).toBe(`Instructions from: ${path.join(dir, "ALKERA.md")}\n# Alkera Rules`)
        expect(rules[1]).toBe(`Instructions from: ${path.join(dir, "AGENTS.md")}\n# Agents Rules`)
      }),
    ),
  )

  it.live("attaches subdir ALKERA.md (first) additively with the subdir AGENTS.md on file read", () =>
    withFiles(
      {
        "subdir/ALKERA.md": "# Subdir Alkera",
        "subdir/AGENTS.md": "# Subdir Agents",
        "subdir/nested/file.ts": "const x = 1",
      },
      (dir) =>
        Effect.gen(function* () {
          const svc = yield* Instruction.Service
          const results = yield* svc.resolve(
            [],
            path.join(dir, "subdir", "nested", "file.ts"),
            MessageID.make("msg_message-alkera-1"),
          )
          expect(results.map((r) => r.filepath)).toEqual([
            path.join(dir, "subdir", "ALKERA.md"),
            path.join(dir, "subdir", "AGENTS.md"),
          ])
        }),
    ),
  )

  it.live("ALKERA_DISABLE_PROJECT_INSTRUCTIONS skips project discovery but keeps global files", () =>
    Effect.gen(function* () {
      const globalTmp = yield* tmpWithFiles({ "AGENTS.md": "# Global Agents" })
      const projectTmp = yield* tmpWithFiles({ "AGENTS.md": "# Project Agents", "ALKERA.md": "# Project Alkera" })

      const prev = process.env.ALKERA_DISABLE_PROJECT_INSTRUCTIONS
      process.env.ALKERA_DISABLE_PROJECT_INSTRUCTIONS = "true"
      try {
        yield* Effect.gen(function* () {
          const svc = yield* Instruction.Service
          const paths = yield* svc.systemPaths()
          expect(paths.has(path.join(globalTmp, "AGENTS.md"))).toBe(true)
          expect(paths.has(path.join(projectTmp, "AGENTS.md"))).toBe(false)
          expect(paths.has(path.join(projectTmp, "ALKERA.md"))).toBe(false)
        }).pipe(provideInstance(projectTmp), provideInstruction({ home: globalTmp, config: globalTmp }))
      } finally {
        if (prev === undefined) delete process.env.ALKERA_DISABLE_PROJECT_INSTRUCTIONS
        else process.env.ALKERA_DISABLE_PROJECT_INSTRUCTIONS = prev
      }
    }),
  )

  it.live("ALKERA_DISABLE_PROJECT_INSTRUCTIONS also disables the nearby (subdir) walk", () =>
    withFiles(
      { "subdir/ALKERA.md": "# Subdir Alkera", "subdir/nested/file.ts": "const x = 1" },
      (dir) =>
        Effect.gen(function* () {
          // The flag must disable project instruction discovery CONSISTENTLY — both
          // systemPaths() and the per-read resolve() nearby walk (regression guard
          // for find() once ignoring the flag).
          const prev = process.env.ALKERA_DISABLE_PROJECT_INSTRUCTIONS
          process.env.ALKERA_DISABLE_PROJECT_INSTRUCTIONS = "true"
          try {
            const svc = yield* Instruction.Service
            const results = yield* svc.resolve(
              [],
              path.join(dir, "subdir", "nested", "file.ts"),
              MessageID.make("msg_message-flag-nearby-1"),
            )
            expect(results).toEqual([])
          } finally {
            if (prev === undefined) delete process.env.ALKERA_DISABLE_PROJECT_INSTRUCTIONS
            else process.env.ALKERA_DISABLE_PROJECT_INSTRUCTIONS = prev
          }
        }),
    ),
  )
})

describe("Instruction size caps", () => {
  it.live("head-truncates an oversized instruction file with a marker", () =>
    withFiles({ "AGENTS.md": "X".repeat(40 * 1024) }, (dir) =>
      Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const rules = yield* svc.system()
        expect(rules).toHaveLength(1)
        expect(rules[0]).toContain("truncated: showing first 32KB of 40KB")
        // The block is bounded near the 32KB per-file cap (+ header + marker).
        expect(new TextEncoder().encode(rules[0]).length).toBeLessThan(33 * 1024 + 300)
      }),
    ),
  )

  it.live("truncates the lower-priority file at the total budget while ALKERA.md keeps its head", () =>
    withFiles({ "ALKERA.md": "A".repeat(50 * 1024), "AGENTS.md": "B".repeat(50 * 1024) }, (dir) =>
      Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const rules = yield* svc.system()
        expect(rules).toHaveLength(2)
        // ALKERA.md leads (precedence) and retains its full per-file head — the TOTAL
        // budget did not trim it (the 32KB per-file head survives intact).
        expect(rules[0]).toContain(`Instructions from: ${path.join(dir, "ALKERA.md")}`)
        expect(new TextEncoder().encode(rules[0]).length).toBeGreaterThanOrEqual(32 * 1024)
        // AGENTS.md is the file actually cut at the budget boundary.
        expect(rules[1]).toContain(`Instructions from: ${path.join(dir, "AGENTS.md")}`)
        expect(rules[1]).toContain("truncated")
        const total = rules.reduce((n, r) => n + new TextEncoder().encode(r).length, 0)
        // The whole assembled set stays near the 64KB total budget (+ small overhead).
        expect(total).toBeLessThan(66 * 1024)
      }),
    ),
  )

  it.live("omits the lowest-priority files past the total budget with a (singular) note", () =>
    Effect.gen(function* () {
      // Three ~32KB instruction files (global AGENTS + project ALKERA + project AGENTS)
      // exceed the 64KB total: ALKERA.md leads intact, the next is truncated to the
      // remainder, and the third is dropped with a count note.
      const globalTmp = yield* tmpWithFiles({ "AGENTS.md": "G".repeat(32 * 1024) })
      const projectTmp = yield* tmpWithFiles({
        "ALKERA.md": "A".repeat(32 * 1024),
        "AGENTS.md": "P".repeat(32 * 1024),
      })

      yield* Effect.gen(function* () {
        const svc = yield* Instruction.Service
        const rules = yield* svc.system()
        // ALKERA.md leads and is intact even under omission (precedence preserved).
        expect(rules[0]).toContain(`Instructions from: ${path.join(projectTmp, "ALKERA.md")}`)
        expect(rules[0]).toContain("A".repeat(200))
        expect(rules[0]).not.toContain("truncated")
        // Exactly one file dropped → singular wording, as the final entry.
        expect(rules[rules.length - 1]).toBe(
          "[... 1 further instruction file omitted: total instruction budget (64KB) reached ...]",
        )
      }).pipe(provideInstance(projectTmp), provideInstruction({ home: globalTmp, config: globalTmp }))
    }),
  )
})
// == ALKERA EDIT END
