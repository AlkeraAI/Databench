import { afterEach, describe, expect, mock, test } from "bun:test"
import { APICallError } from "ai"
import { Cause, Deferred, Effect, Exit, Fiber, Layer, Schema } from "effect"
import * as Stream from "effect/Stream"
import { Bus } from "../../src/bus"
import { Config } from "@/config/config"
import { Image } from "@/image/image"
import { Agent } from "../../src/agent/agent"
import { LLM } from "../../src/session/llm"
import { SessionCompaction } from "../../src/session/compaction"
import { Token } from "@/util/token"
import * as Log from "@opencode-ai/core/util/log"
import { Permission } from "../../src/permission"
import { Plugin } from "../../src/plugin"
import { provideTmpdirInstance, TestInstance } from "../fixture/fixture"
import { Session as SessionNs } from "@/session/session"
import { MessageV2 } from "../../src/session/message-v2"
import { MessageID, PartID, SessionID } from "../../src/session/schema"
import { SessionStatus } from "../../src/session/status"
import { SessionSummary } from "../../src/session/summary"
import { SessionV2 } from "../../src/v2/session"
import { ModelID, ProviderID } from "../../src/provider/schema"
import type { Provider } from "@/provider/provider"
import * as SessionProcessorModule from "../../src/session/processor"
import { Snapshot } from "../../src/snapshot"
import { ProviderTest } from "../fake/provider"
import { testEffect } from "../lib/effect"
import { CrossSpawnSpawner } from "@opencode-ai/core/cross-spawn-spawner"
import { TestConfig } from "../fixture/config"
import { SyncEvent } from "@/sync"
import { RuntimeFlags } from "@/effect/runtime-flags"
import { EventV2Bridge } from "@/event-v2-bridge"
import { LLMEvent, Usage } from "@opencode-ai/llm"

void Log.init({ print: false })

const summary = Layer.succeed(
  SessionSummary.Service,
  SessionSummary.Service.of({
    summarize: () => Effect.void,
    diff: () => Effect.succeed([]),
    computeDiff: () => Effect.succeed([]),
  }),
)

const ref = {
  providerID: ProviderID.make("test"),
  modelID: ModelID.make("test-model"),
}

const usage = (input: ConstructorParameters<typeof Usage>[0]) => new Usage(input)

const basicUsage = () => usage({ inputTokens: 1, outputTokens: 1, totalTokens: 2 })

afterEach(() => {
  mock.restore()
})

function createModel(opts: {
  context: number
  output: number
  input?: number
  cost?: Provider.Model["cost"]
  npm?: string
}): Provider.Model {
  return {
    id: "test-model",
    providerID: "test",
    name: "Test",
    limit: {
      context: opts.context,
      input: opts.input,
      output: opts.output,
    },
    cost: opts.cost ?? { input: 0, output: 0, cache: { read: 0, write: 0 } },
    capabilities: {
      toolcall: true,
      attachment: false,
      reasoning: false,
      temperature: true,
      input: { text: true, image: false, audio: false, video: false },
      output: { text: true, image: false, audio: false, video: false },
    },
    api: { npm: opts.npm ?? "@ai-sdk/anthropic" },
    options: {},
  } as Provider.Model
}

const wide = () => ProviderTest.fake({ model: createModel({ context: 100_000, output: 32_000 }) })

function createUserMessage(sessionID: SessionID, text: string) {
  return Effect.gen(function* () {
    const ssn = yield* SessionNs.Service
    const msg = yield* ssn.updateMessage({
      id: MessageID.ascending(),
      role: "user",
      sessionID,
      agent: "build",
      model: ref,
      time: { created: Date.now() },
    })
    yield* ssn.updatePart({
      id: PartID.ascending(),
      messageID: msg.id,
      sessionID,
      type: "text",
      text,
    })
    return msg
  })
}

function createAssistantMessage(sessionID: SessionID, parentID: MessageID, root: string) {
  return SessionNs.Service.use((ssn) =>
    ssn.updateMessage({
      id: MessageID.ascending(),
      role: "assistant",
      sessionID,
      mode: "build",
      agent: "build",
      path: { cwd: root, root },
      cost: 0,
      tokens: {
        output: 0,
        input: 0,
        reasoning: 0,
        cache: { read: 0, write: 0 },
      },
      modelID: ref.modelID,
      providerID: ref.providerID,
      parentID,
      time: { created: Date.now() },
      finish: "end_turn",
    }),
  )
}

function createSummaryAssistantMessage(sessionID: SessionID, parentID: MessageID, root: string, text: string) {
  return SessionNs.Service.use((ssn) =>
    Effect.gen(function* () {
      const msg = yield* ssn.updateMessage({
        id: MessageID.ascending(),
        role: "assistant",
        sessionID,
        mode: "compaction",
        agent: "compaction",
        path: { cwd: root, root },
        cost: 0,
        tokens: {
          output: 0,
          input: 0,
          reasoning: 0,
          cache: { read: 0, write: 0 },
        },
        modelID: ref.modelID,
        providerID: ref.providerID,
        parentID,
        summary: true,
        time: { created: Date.now() },
        finish: "end_turn",
      })
      yield* ssn.updatePart({
        id: PartID.ascending(),
        messageID: msg.id,
        sessionID,
        type: "text",
        text,
      })
      return msg
    }),
  )
}

function createCompactionMarker(sessionID: SessionID) {
  return SessionNs.Service.use((ssn) =>
    Effect.gen(function* () {
      const msg = yield* ssn.updateMessage({
        id: MessageID.ascending(),
        role: "user",
        model: ref,
        sessionID,
        agent: "build",
        time: { created: Date.now() },
      })
      yield* ssn.updatePart({
        id: PartID.ascending(),
        messageID: msg.id,
        sessionID: msg.sessionID,
        type: "compaction",
        auto: false,
      })
    }),
  )
}

function fake(
  input: Parameters<SessionProcessorModule.SessionProcessor.Interface["create"]>[0],
  result: "continue" | "compact",
) {
  const msg = input.assistantMessage
  return {
    get message() {
      return msg
    },
    updateToolCall: Effect.fn("TestSessionProcessor.updateToolCall")(() => Effect.succeed(undefined)),
    completeToolCall: Effect.fn("TestSessionProcessor.completeToolCall")(() => Effect.void),
    process: Effect.fn("TestSessionProcessor.process")(() => Effect.succeed(result)),
  } satisfies SessionProcessorModule.SessionProcessor.Handle
}

function layer(result: "continue" | "compact") {
  return Layer.succeed(
    SessionProcessorModule.SessionProcessor.Service,
    SessionProcessorModule.SessionProcessor.Service.of({
      create: Effect.fn("TestSessionProcessor.create")((input) => Effect.succeed(fake(input, result))),
    }),
  )
}

function cfg(compaction?: Config.Info["compaction"]) {
  const base = Schema.decodeUnknownSync(Config.Info)({}) as Config.Info
  return TestConfig.layer({
    get: () => Effect.succeed({ ...base, compaction }),
  })
}

const deps = Layer.mergeAll(
  wide().layer,
  layer("continue"),
  Agent.defaultLayer,
  Plugin.defaultLayer,
  Bus.layer,
  Config.defaultLayer,
  SyncEvent.defaultLayer,
  RuntimeFlags.layer({ experimentalEventSystem: true }),
  EventV2Bridge.defaultLayer,
)

const env = Layer.mergeAll(
  SessionNs.defaultLayer,
  CrossSpawnSpawner.defaultLayer,
  SessionCompaction.layer.pipe(Layer.provide(SessionNs.defaultLayer), Layer.provideMerge(deps)),
)

const it = testEffect(env)

const compactionEnv = Layer.mergeAll(SessionNs.defaultLayer, CrossSpawnSpawner.defaultLayer)
const itCompaction = testEffect(compactionEnv)

type CompactionProcessOptions = {
  result?: "continue" | "compact"
  llm?: Layer.Layer<LLM.Service>
  plugin?: Layer.Layer<Plugin.Service>
  provider?: ReturnType<typeof ProviderTest.fake>
  config?: Layer.Layer<Config.Service>
}

function withCompaction(options?: CompactionProcessOptions) {
  return Effect.provide(compactionProcessLayer(options))
}

function compactionProcessLayer(options?: CompactionProcessOptions) {
  const bus = Bus.layer
  const status = SessionStatus.layer.pipe(Layer.provide(bus))
  const processor = options?.llm
    ? SessionProcessorModule.SessionProcessor.layer.pipe(
        Layer.provide(summary),
        Layer.provide(Image.defaultLayer),
        Layer.provide(RuntimeFlags.layer({ experimentalEventSystem: true })),
        Layer.provide(status),
      )
    : layer(options?.result ?? "continue")
  return Layer.mergeAll(SessionCompaction.layer.pipe(Layer.provide(processor)), processor, bus, status).pipe(
    Layer.provide(SessionNs.defaultLayer),
    Layer.provide((options?.provider ?? wide()).layer),
    Layer.provide(Snapshot.defaultLayer),
    Layer.provide(options?.llm ?? LLM.defaultLayer),
    Layer.provide(Permission.defaultLayer),
    Layer.provide(Agent.defaultLayer),
    Layer.provide(options?.plugin ?? Plugin.defaultLayer),
    Layer.provide(status),
    Layer.provide(bus),
    Layer.provide(options?.config ?? Config.defaultLayer),
    Layer.provide(SyncEvent.defaultLayer),
    Layer.provide(RuntimeFlags.layer({ experimentalEventSystem: true })),
    Layer.provide(EventV2Bridge.defaultLayer),
  )
}

function createSummaryCompaction(sessionID: SessionID) {
  return SessionCompaction.use.create({ sessionID, agent: "build", model: ref, auto: false })
}

function readCompactionPart(sessionID: SessionID) {
  return SessionNs.use
    .messages({ sessionID })
    .pipe(
      Effect.map((messages) =>
        messages.at(-2)?.parts.find((item): item is MessageV2.CompactionPart => item.type === "compaction"),
      ),
    )
}

function llm() {
  const queue: Array<
    Stream.Stream<LLMEvent, unknown> | ((input: LLM.StreamInput) => Stream.Stream<LLMEvent, unknown>)
  > = []

  return {
    push(stream: Stream.Stream<LLMEvent, unknown> | ((input: LLM.StreamInput) => Stream.Stream<LLMEvent, unknown>)) {
      queue.push(stream)
    },
    layer: Layer.succeed(
      LLM.Service,
      LLM.Service.of({
        stream: (input) => {
          const item = queue.shift() ?? Stream.empty
          const stream = typeof item === "function" ? item(input) : item
          return stream.pipe(Stream.mapEffect((event) => Effect.succeed(event)))
        },
      }),
    ),
  }
}

function reply(
  text: string,
  capture?: (input: LLM.StreamInput) => void,
): (input: LLM.StreamInput) => Stream.Stream<LLMEvent, unknown> {
  return (input) => {
    capture?.(input)
    return Stream.make(
      LLMEvent.textStart({ id: "txt-0" }),
      LLMEvent.textDelta({ id: "txt-0", text }),
      LLMEvent.textEnd({ id: "txt-0" }),
      LLMEvent.stepFinish({
        index: 0,
        reason: "stop",
        usage: basicUsage(),
      }),
      LLMEvent.finish({
        reason: "stop",
        usage: basicUsage(),
      }),
    )
  }
}

function plugin(ready: Deferred.Deferred<void>) {
  return Layer.mock(Plugin.Service)({
    trigger: <Name extends string, Input, Output>(name: Name, _input: Input, output: Output) => {
      if (name !== "experimental.session.compacting") return Effect.succeed(output)
      return Effect.sync(() => Deferred.doneUnsafe(ready, Effect.void)).pipe(
        Effect.andThen(Effect.never),
        Effect.as(output),
      )
    },
    list: () => Effect.succeed([]),
    init: () => Effect.void,
  })
}

function autocontinue(enabled: boolean) {
  return Layer.mock(Plugin.Service)({
    trigger: <Name extends string, Input, Output>(name: Name, _input: Input, output: Output) => {
      if (name !== "experimental.compaction.autocontinue") return Effect.succeed(output)
      return Effect.sync(() => {
        ;(output as { enabled: boolean }).enabled = enabled
        return output
      })
    },
    list: () => Effect.succeed([]),
    init: () => Effect.void,
  })
}

describe("session.compaction.isOverflow", () => {
  it.live(
    "returns true when token count exceeds usable context",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        const model = createModel({ context: 100_000, output: 32_000 })
        const tokens = { input: 75_000, output: 5_000, reasoning: 0, cache: { read: 0, write: 0 } }
        expect(yield* compact.isOverflow({ tokens, model })).toBe(true)
      }),
    ),
  )

  it.live(
    "returns false when token count within usable context",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        const model = createModel({ context: 200_000, output: 32_000 })
        const tokens = { input: 100_000, output: 10_000, reasoning: 0, cache: { read: 0, write: 0 } }
        expect(yield* compact.isOverflow({ tokens, model })).toBe(false)
      }),
    ),
  )

  it.live(
    "includes cache.read in token count",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        const model = createModel({ context: 100_000, output: 32_000 })
        const tokens = { input: 60_000, output: 10_000, reasoning: 0, cache: { read: 10_000, write: 0 } }
        expect(yield* compact.isOverflow({ tokens, model })).toBe(true)
      }),
    ),
  )

  it.live(
    "respects input limit for input caps",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        const model = createModel({ context: 400_000, input: 272_000, output: 128_000 })
        const tokens = { input: 271_000, output: 1_000, reasoning: 0, cache: { read: 2_000, write: 0 } }
        expect(yield* compact.isOverflow({ tokens, model })).toBe(true)
      }),
    ),
  )

  it.live(
    "returns false when input/output are within input caps",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        const model = createModel({ context: 400_000, input: 272_000, output: 128_000 })
        const tokens = { input: 200_000, output: 20_000, reasoning: 0, cache: { read: 10_000, write: 0 } }
        expect(yield* compact.isOverflow({ tokens, model })).toBe(false)
      }),
    ),
  )

  it.live(
    "returns false when output within limit with input caps",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        const model = createModel({ context: 200_000, input: 120_000, output: 10_000 })
        const tokens = { input: 50_000, output: 9_999, reasoning: 0, cache: { read: 0, write: 0 } }
        expect(yield* compact.isOverflow({ tokens, model })).toBe(false)
      }),
    ),
  )

  // ─── Bug reproduction tests ───────────────────────────────────────────
  // These tests demonstrate that when limit.input is set, isOverflow()
  // does not subtract any headroom for the next model response. This means
  // compaction only triggers AFTER we've already consumed the full input
  // budget, leaving zero room for the next API call's output tokens.
  //
  // Compare: without limit.input, usable = context - output (reserves space).
  // With limit.input, usable = limit.input (reserves nothing).
  //
  // Related issues: #10634, #8089, #11086, #12621
  // Open PRs: #6875, #12924

  it.live(
    "BUG: no headroom when limit.input is set — compaction should trigger near boundary but does not",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        // Simulate Claude with prompt caching: input limit = 200K, output limit = 32K
        const model = createModel({ context: 200_000, input: 200_000, output: 32_000 })

        // We've used 198K tokens total. Only 2K under the input limit.
        // On the next turn, the full conversation (198K) becomes input,
        // plus the model needs room to generate output — this WILL overflow.
        const tokens = { input: 180_000, output: 15_000, reasoning: 0, cache: { read: 3_000, write: 0 } }
        // count = 180K + 3K + 15K = 198K
        // usable = limit.input = 200K (no output subtracted!)
        // 198K > 200K = false → no compaction triggered

        // WITHOUT limit.input: usable = 200K - 32K = 168K, and 198K > 168K = true ✓
        // WITH limit.input: usable = 200K, and 198K > 200K = false ✗

        // With 198K used and only 2K headroom, the next turn will overflow.
        // Compaction MUST trigger here.
        expect(yield* compact.isOverflow({ tokens, model })).toBe(true)
      }),
    ),
  )

  it.live(
    "BUG: without limit.input, same token count correctly triggers compaction",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        // Same model but without limit.input — uses context - output instead
        const model = createModel({ context: 200_000, output: 32_000 })

        // Same token usage as above
        const tokens = { input: 180_000, output: 15_000, reasoning: 0, cache: { read: 3_000, write: 0 } }
        // count = 198K
        // usable = context - output = 200K - 32K = 168K
        // 198K > 168K = true → compaction correctly triggered

        const result = yield* compact.isOverflow({ tokens, model })
        expect(result).toBe(true) // ← Correct: headroom is reserved
      }),
    ),
  )

  it.live(
    "BUG: asymmetry — limit.input model allows 30K more usage before compaction than equivalent model without it",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        // Two models with identical context/output limits, differing only in limit.input
        const withInputLimit = createModel({ context: 200_000, input: 200_000, output: 32_000 })
        const withoutInputLimit = createModel({ context: 200_000, output: 32_000 })

        // 170K total tokens — well above context-output (168K) but below input limit (200K)
        const tokens = { input: 166_000, output: 10_000, reasoning: 0, cache: { read: 5_000, write: 0 } }

        const withLimit = yield* compact.isOverflow({ tokens, model: withInputLimit })
        const withoutLimit = yield* compact.isOverflow({ tokens, model: withoutInputLimit })

        // Both models have identical real capacity — they should agree:
        expect(withLimit).toBe(true) // should compact (170K leaves no room for 32K output)
        expect(withoutLimit).toBe(true) // correctly compacts (170K > 168K)
      }),
    ),
  )

  it.live(
    "returns false when model context limit is 0",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        const model = createModel({ context: 0, output: 32_000 })
        const tokens = { input: 100_000, output: 10_000, reasoning: 0, cache: { read: 0, write: 0 } }
        expect(yield* compact.isOverflow({ tokens, model })).toBe(false)
      }),
    ),
  )

  it.live(
    "returns false when compaction.auto is disabled",
    provideTmpdirInstance(
      () =>
        Effect.gen(function* () {
          const compact = yield* SessionCompaction.Service
          const model = createModel({ context: 100_000, output: 32_000 })
          const tokens = { input: 75_000, output: 5_000, reasoning: 0, cache: { read: 0, write: 0 } }
          expect(yield* compact.isOverflow({ tokens, model })).toBe(false)
        }),
      {
        config: {
          compaction: { auto: false },
        },
      },
    ),
  )
})

describe("session.compaction.create", () => {
  it.live(
    "creates a compaction user message and part",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        const ssn = yield* SessionNs.Service

        const info = yield* ssn.create({})

        yield* compact.create({
          sessionID: info.id,
          agent: "build",
          model: ref,
          auto: true,
          overflow: true,
        })

        const msgs = yield* ssn.messages({ sessionID: info.id })
        expect(msgs).toHaveLength(1)
        expect(msgs[0].info.role).toBe("user")
        expect(msgs[0].parts).toHaveLength(1)
        expect(msgs[0].parts[0]).toMatchObject({
          type: "compaction",
          auto: true,
          overflow: true,
        })

        const v2 = yield* SessionV2.Service.use((svc) => svc.messages({ sessionID: info.id })).pipe(
          Effect.provide(SessionV2.defaultLayer),
        )
        expect(v2.at(-1)).toMatchObject({
          type: "compaction",
          reason: "auto",
          summary: "",
        })
      }),
    ),
  )
})

describe("session.compaction.prune", () => {
  it.live(
    "compacts old completed tool output",
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          const compact = yield* SessionCompaction.Service
          const ssn = yield* SessionNs.Service
          const info = yield* ssn.create({})
          const a = yield* ssn.updateMessage({
            id: MessageID.ascending(),
            role: "user",
            sessionID: info.id,
            agent: "build",
            model: ref,
            time: { created: Date.now() },
          })
          yield* ssn.updatePart({
            id: PartID.ascending(),
            messageID: a.id,
            sessionID: info.id,
            type: "text",
            text: "first",
          })
          const b: MessageV2.Assistant = {
            id: MessageID.ascending(),
            role: "assistant",
            sessionID: info.id,
            mode: "build",
            agent: "build",
            path: { cwd: dir, root: dir },
            cost: 0,
            tokens: {
              output: 0,
              input: 0,
              reasoning: 0,
              cache: { read: 0, write: 0 },
            },
            modelID: ref.modelID,
            providerID: ref.providerID,
            parentID: a.id,
            time: { created: Date.now() },
            finish: "end_turn",
          }
          yield* ssn.updateMessage(b)
          yield* ssn.updatePart({
            id: PartID.ascending(),
            messageID: b.id,
            sessionID: info.id,
            type: "tool",
            callID: crypto.randomUUID(),
            tool: "bash",
            state: {
              status: "completed",
              input: {},
              output: "x".repeat(200_000),
              title: "done",
              metadata: {},
              time: { start: Date.now(), end: Date.now() },
            },
          })
          for (const text of ["second", "third"]) {
            const msg = yield* ssn.updateMessage({
              id: MessageID.ascending(),
              role: "user",
              sessionID: info.id,
              agent: "build",
              model: ref,
              time: { created: Date.now() },
            })
            yield* ssn.updatePart({
              id: PartID.ascending(),
              messageID: msg.id,
              sessionID: info.id,
              type: "text",
              text,
            })
          }

          yield* compact.prune({ sessionID: info.id })

          const msgs = yield* ssn.messages({ sessionID: info.id })
          const part = msgs.flatMap((msg) => msg.parts).find((part) => part.type === "tool")
          expect(part?.type).toBe("tool")
          expect(part?.state.status).toBe("completed")
          if (part?.type === "tool" && part.state.status === "completed") {
            expect(part.state.time.compacted).toBeNumber()
          }
        }),

      {
        config: {
          compaction: { prune: true },
        },
      },
    ),
  )

  it.live(
    "skips protected skill tool output",
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        const compact = yield* SessionCompaction.Service
        const ssn = yield* SessionNs.Service
        const info = yield* ssn.create({})
        const a = yield* ssn.updateMessage({
          id: MessageID.ascending(),
          role: "user",
          sessionID: info.id,
          agent: "build",
          model: ref,
          time: { created: Date.now() },
        })
        yield* ssn.updatePart({
          id: PartID.ascending(),
          messageID: a.id,
          sessionID: info.id,
          type: "text",
          text: "first",
        })
        const b: MessageV2.Assistant = {
          id: MessageID.ascending(),
          role: "assistant",
          sessionID: info.id,
          mode: "build",
          agent: "build",
          path: { cwd: dir, root: dir },
          cost: 0,
          tokens: {
            output: 0,
            input: 0,
            reasoning: 0,
            cache: { read: 0, write: 0 },
          },
          modelID: ref.modelID,
          providerID: ref.providerID,
          parentID: a.id,
          time: { created: Date.now() },
          finish: "end_turn",
        }
        yield* ssn.updateMessage(b)
        yield* ssn.updatePart({
          id: PartID.ascending(),
          messageID: b.id,
          sessionID: info.id,
          type: "tool",
          callID: crypto.randomUUID(),
          tool: "skill",
          state: {
            status: "completed",
            input: {},
            output: "x".repeat(200_000),
            title: "done",
            metadata: {},
            time: { start: Date.now(), end: Date.now() },
          },
        })
        for (const text of ["second", "third"]) {
          const msg = yield* ssn.updateMessage({
            id: MessageID.ascending(),
            role: "user",
            sessionID: info.id,
            agent: "build",
            model: ref,
            time: { created: Date.now() },
          })
          yield* ssn.updatePart({
            id: PartID.ascending(),
            messageID: msg.id,
            sessionID: info.id,
            type: "text",
            text,
          })
        }

        yield* compact.prune({ sessionID: info.id })

        const msgs = yield* ssn.messages({ sessionID: info.id })
        const part = msgs.flatMap((msg) => msg.parts).find((part) => part.type === "tool")
        expect(part?.type).toBe("tool")
        if (part?.type === "tool" && part.state.status === "completed") {
          expect(part.state.time.compacted).toBeUndefined()
        }
      }),
    ),
  )
})

describe("session.compaction.process", () => {
  it.instance(
    "throws when parent is not a user message",
    Effect.gen(function* () {
      const test = yield* TestInstance
      const ssn = yield* SessionNs.Service
      const session = yield* ssn.create({})
      const msg = yield* createUserMessage(session.id, "hello")
      const reply = yield* createAssistantMessage(session.id, msg.id, test.directory)
      const msgs = yield* ssn.messages({ sessionID: session.id })

      const exit = yield* Effect.exit(
        SessionCompaction.use.process({
          parentID: reply.id,
          messages: msgs,
          sessionID: session.id,
          auto: false,
        }),
      )

      expect(Exit.isFailure(exit)).toBe(true)
      if (Exit.isFailure(exit)) {
        const error = Cause.squash(exit.cause)
        expect(error).toBeInstanceOf(Error)
        if (error instanceof Error) {
          expect(error.message).toContain(`Compaction parent must be a user message: ${reply.id}`)
        }
      }
    }),
  )

  it.instance(
    "publishes compacted event on continue",
    Effect.gen(function* () {
      const bus = yield* Bus.Service
      const ssn = yield* SessionNs.Service
      const session = yield* ssn.create({})
      const msg = yield* createUserMessage(session.id, "hello")
      const msgs = yield* ssn.messages({ sessionID: session.id })
      const done = yield* Deferred.make<void, Error>()
      let seen = false
      const unsub = yield* bus.subscribeCallback(SessionCompaction.Event.Compacted, (evt) => {
        if (evt.properties.sessionID !== session.id) return
        seen = true
        Deferred.doneUnsafe(done, Effect.void)
      })
      yield* Effect.addFinalizer(() => Effect.sync(unsub))

      const result = yield* SessionCompaction.use.process({
        parentID: msg.id,
        messages: msgs,
        sessionID: session.id,
        auto: false,
      })

      yield* Deferred.await(done).pipe(Effect.timeout("500 millis"))
      expect(result).toBe("continue")
      expect(seen).toBe(true)
    }),
  )

  itCompaction.instance(
    "marks summary message as errored on compact result",
    Effect.gen(function* () {
      const ssn = yield* SessionNs.Service
      const session = yield* ssn.create({})
      const msg = yield* createUserMessage(session.id, "hello")
      const msgs = yield* ssn.messages({ sessionID: session.id })

      const result = yield* SessionCompaction.use.process({
        parentID: msg.id,
        messages: msgs,
        sessionID: session.id,
        auto: false,
      })

      const summary = (yield* ssn.messages({ sessionID: session.id })).find(
        (msg) => msg.info.role === "assistant" && msg.info.summary,
      )

      expect(result).toBe("stop")
      expect(summary?.info.role).toBe("assistant")
      if (summary?.info.role === "assistant") {
        expect(summary.info.finish).toBe("error")
        expect(JSON.stringify(summary.info.error)).toContain("Session too large to compact")
      }
    }).pipe(withCompaction({ result: "compact" })),
  )

  // == ALKERA EDIT START: the history has to be long enough for the interrupted request
  // to be RETAINED after the boundary (two earlier turns fold, the newest stays);
  // a lone unanswered prompt is re-attached instead of nudged (see the alkera block).
  it.instance(
    "adds synthetic continue prompt when auto is enabled",
    Effect.gen(function* () {
      const test = yield* TestInstance
      const ssn = yield* SessionNs.Service
      const session = yield* ssn.create({})
      const first = yield* createUserMessage(session.id, "first")
      yield* createAssistantMessage(session.id, first.id, test.directory)
      const second = yield* createUserMessage(session.id, "second")
      yield* createAssistantMessage(session.id, second.id, test.directory)
      const msg = yield* createUserMessage(session.id, "hello")
      const msgs = yield* ssn.messages({ sessionID: session.id })

      const result = yield* SessionCompaction.use.process({
        parentID: msg.id,
        messages: msgs,
        sessionID: session.id,
        auto: true,
      })

      const all = yield* ssn.messages({ sessionID: session.id })
      const last = all.at(-1)

      expect(result).toBe("continue")
      expect(last?.info.role).toBe("user")
      expect(last?.parts[0]).toMatchObject({
        type: "text",
        synthetic: true,
        metadata: { compaction_continue: true },
      })
      if (last?.parts[0]?.type === "text") {
        expect(last.parts[0].text).toContain("Do not ask what to do next")
        expect(last.parts[0].text).not.toContain("stop and ask for clarification")
      }
    }),
  )
  // == ALKERA EDIT END

  itCompaction.instance(
    "persists tail_start_id for retained recent turns",
    Effect.gen(function* () {
      const ssn = yield* SessionNs.Service
      const session = yield* ssn.create({})
      yield* createUserMessage(session.id, "first")
      const keep = yield* createUserMessage(session.id, "second")
      yield* createUserMessage(session.id, "third")
      yield* createSummaryCompaction(session.id)

      const msgs = yield* ssn.messages({ sessionID: session.id })
      const parent = msgs.at(-1)?.info.id
      expect(parent).toBeTruthy()
      yield* SessionCompaction.use.process({
        parentID: parent!,
        messages: msgs,
        sessionID: session.id,
        auto: false,
      })

      const part = yield* readCompactionPart(session.id)
      expect(part?.type).toBe("compaction")
      expect(part?.tail_start_id).toBe(keep.id)
    }).pipe(withCompaction({ config: cfg({ tail_turns: 2, preserve_recent_tokens: 10_000 }) })),
  )

  itCompaction.instance(
    "shrinks retained tail to fit preserve token budget",
    Effect.gen(function* () {
      const ssn = yield* SessionNs.Service
      const session = yield* ssn.create({})
      yield* createUserMessage(session.id, "first")
      yield* createUserMessage(session.id, "x".repeat(2_000))
      const keep = yield* createUserMessage(session.id, "tiny")
      yield* createSummaryCompaction(session.id)

      const msgs = yield* ssn.messages({ sessionID: session.id })
      const parent = msgs.at(-1)?.info.id
      expect(parent).toBeTruthy()
      yield* SessionCompaction.use.process({
        parentID: parent!,
        messages: msgs,
        sessionID: session.id,
        auto: false,
      })

      const part = yield* readCompactionPart(session.id)
      expect(part?.type).toBe("compaction")
      expect(part?.tail_start_id).toBe(keep.id)
    }).pipe(withCompaction({ config: cfg({ tail_turns: 2, preserve_recent_tokens: 100 }) })),
  )

  itCompaction.instance(
    "falls back to full summary when even one recent turn exceeds preserve token budget",
    () => {
      const stub = llm()
      let captured = ""
      stub.push(reply("summary", (input) => (captured = JSON.stringify(input.messages))))
      return Effect.gen(function* () {
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        yield* createUserMessage(session.id, "first")
        yield* createUserMessage(session.id, "y".repeat(2_000))
        yield* createSummaryCompaction(session.id)

        const msgs = yield* ssn.messages({ sessionID: session.id })
        const parent = msgs.at(-1)?.info.id
        expect(parent).toBeTruthy()
        yield* SessionCompaction.use.process({ parentID: parent!, messages: msgs, sessionID: session.id, auto: false })

        const part = yield* readCompactionPart(session.id)
        expect(part?.type).toBe("compaction")
        expect(part?.tail_start_id).toBeUndefined()
        expect(captured).toContain("yyyy")
      }).pipe(withCompaction({ llm: stub.layer, config: cfg({ tail_turns: 1, preserve_recent_tokens: 20 }) }))
    },
    { git: true },
  )

  itCompaction.instance(
    "falls back to full summary when retained tail media exceeds preserve token budget",
    () => {
      const stub = llm()
      let captured = ""
      stub.push(reply("summary", (input) => (captured = JSON.stringify(input.messages))))
      return Effect.gen(function* () {
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        yield* createUserMessage(session.id, "older")
        const recent = yield* createUserMessage(session.id, "recent image turn")
        yield* ssn.updatePart({
          id: PartID.ascending(),
          messageID: recent.id,
          sessionID: session.id,
          type: "file",
          mime: "image/png",
          filename: "big.png",
          url: `data:image/png;base64,${"a".repeat(4_000)}`,
        })
        yield* createSummaryCompaction(session.id)

        const msgs = yield* ssn.messages({ sessionID: session.id })
        const parent = msgs.at(-1)?.info.id
        expect(parent).toBeTruthy()
        yield* SessionCompaction.use.process({ parentID: parent!, messages: msgs, sessionID: session.id, auto: false })

        const part = yield* readCompactionPart(session.id)
        expect(part?.type).toBe("compaction")
        expect(part?.tail_start_id).toBeUndefined()
        expect(captured).toContain("recent image turn")
        expect(captured).toContain("Attached image/png: big.png")
      }).pipe(withCompaction({ llm: stub.layer, config: cfg({ tail_turns: 1, preserve_recent_tokens: 100 }) }))
    },
    { git: true },
  )

  itCompaction.instance(
    "retains a split turn suffix when a later message fits the preserve token budget",
    () => {
      const stub = llm()
      let captured = ""
      stub.push(reply("summary", (input) => (captured = JSON.stringify(input.messages))))
      return Effect.gen(function* () {
        const test = yield* TestInstance
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        yield* createUserMessage(session.id, "older")
        const recent = yield* createUserMessage(session.id, "recent turn")
        const large = yield* createAssistantMessage(session.id, recent.id, test.directory)
        yield* ssn.updatePart({
          id: PartID.ascending(),
          messageID: large.id,
          sessionID: session.id,
          type: "text",
          text: "z".repeat(2_000),
        })
        const keep = yield* createAssistantMessage(session.id, recent.id, test.directory)
        yield* ssn.updatePart({
          id: PartID.ascending(),
          messageID: keep.id,
          sessionID: session.id,
          type: "text",
          text: "keep tail",
        })
        yield* createSummaryCompaction(session.id)

        const msgs = yield* ssn.messages({ sessionID: session.id })
        const parent = msgs.at(-1)?.info.id
        expect(parent).toBeTruthy()
        yield* SessionCompaction.use.process({ parentID: parent!, messages: msgs, sessionID: session.id, auto: false })

        const part = yield* readCompactionPart(session.id)
        expect(part?.type).toBe("compaction")
        expect(part?.tail_start_id).toBe(keep.id)
        expect(captured).toContain("zzzz")
        expect(captured).not.toContain("keep tail")

        const filtered = MessageV2.filterCompacted(MessageV2.stream(session.id))
        expect(filtered.map((msg) => msg.info.id).slice(0, 3)).toEqual([parent!, expect.any(String), keep.id])
        expect(filtered[1]?.info.role).toBe("assistant")
        expect(filtered[1]?.info.role === "assistant" ? filtered[1].info.summary : false).toBe(true)
        expect(filtered.map((msg) => msg.info.id)).not.toContain(large.id)
      }).pipe(withCompaction({ llm: stub.layer, config: cfg({ tail_turns: 1, preserve_recent_tokens: 100 }) }))
    },
    { git: true },
  )

  itCompaction.instance(
    "allows plugins to disable synthetic continue prompt",
    Effect.gen(function* () {
      const test = yield* TestInstance
      const ssn = yield* SessionNs.Service
      const session = yield* ssn.create({})
      // == ALKERA EDIT START: same three-turn shape as the test above — the
      // nudge (and this switch for it) only exists once the request is retained.
      const first = yield* createUserMessage(session.id, "first")
      yield* createAssistantMessage(session.id, first.id, test.directory)
      const second = yield* createUserMessage(session.id, "second")
      yield* createAssistantMessage(session.id, second.id, test.directory)
      // == ALKERA EDIT END
      const msg = yield* createUserMessage(session.id, "hello")
      const msgs = yield* ssn.messages({ sessionID: session.id })

      const result = yield* SessionCompaction.use.process({
        parentID: msg.id,
        messages: msgs,
        sessionID: session.id,
        auto: true,
      })

      const all = yield* ssn.messages({ sessionID: session.id })
      const last = all.at(-1)

      expect(result).toBe("continue")
      expect(last?.info.role).toBe("assistant")
      expect(
        all.some(
          (msg) =>
            msg.info.role === "user" &&
            // == ALKERA EDIT START: the marker, not the wording, names the nudge
            msg.parts.some(
              (part) => part.type === "text" && part.synthetic && part.metadata?.compaction_continue === true,
            ),
          // == ALKERA EDIT END
        ),
      ).toBe(false)
    }).pipe(withCompaction({ plugin: autocontinue(false) })),
  )

  it.instance(
    "replays the prior user turn on overflow when earlier context exists",
    Effect.gen(function* () {
      const ssn = yield* SessionNs.Service
      const session = yield* ssn.create({})
      yield* createUserMessage(session.id, "root")
      const replay = yield* createUserMessage(session.id, "image")
      yield* ssn.updatePart({
        id: PartID.ascending(),
        messageID: replay.id,
        sessionID: session.id,
        type: "file",
        mime: "image/png",
        filename: "cat.png",
        url: "https://example.com/cat.png",
      })
      const msg = yield* createUserMessage(session.id, "current")
      const msgs = yield* ssn.messages({ sessionID: session.id })

      const result = yield* SessionCompaction.use.process({
        parentID: msg.id,
        messages: msgs,
        sessionID: session.id,
        auto: true,
        overflow: true,
      })

      const last = (yield* ssn.messages({ sessionID: session.id })).at(-1)

      expect(result).toBe("continue")
      expect(last?.info.role).toBe("user")
      expect(last?.parts.some((part) => part.type === "file")).toBe(false)
      expect(
        last?.parts.some((part) => part.type === "text" && part.text.includes("Attached image/png: cat.png")),
      ).toBe(true)
    }),
  )

  it.instance(
    "falls back to overflow guidance when no replayable turn exists",
    Effect.gen(function* () {
      const ssn = yield* SessionNs.Service
      const session = yield* ssn.create({})
      yield* createUserMessage(session.id, "earlier")
      const msg = yield* createUserMessage(session.id, "current")
      const msgs = yield* ssn.messages({ sessionID: session.id })

      const result = yield* SessionCompaction.use.process({
        parentID: msg.id,
        messages: msgs,
        sessionID: session.id,
        auto: true,
        overflow: true,
      })

      const last = (yield* ssn.messages({ sessionID: session.id })).at(-1)

      expect(result).toBe("continue")
      expect(last?.info.role).toBe("user")
      if (last?.parts[0]?.type === "text") {
        expect(last.parts[0].text).toContain("previous request exceeded the provider's size limit")
      }
    }),
  )

  itCompaction.instance(
    "stops quickly when aborted during retry backoff",
    () => {
      const stub = llm()
      stub.push(
        Stream.fromAsyncIterable(
          {
            async *[Symbol.asyncIterator]() {
              yield LLMEvent.stepStart({ index: 0 })
              throw new APICallError({
                message: "boom",
                url: "https://example.com/v1/chat/completions",
                requestBodyValues: {},
                statusCode: 503,
                responseHeaders: { "retry-after-ms": "10000" },
                responseBody: '{"error":"boom"}',
                isRetryable: true,
              })
            },
          },
          (err) => err,
        ),
      )

      return Effect.gen(function* () {
        const ssn = yield* SessionNs.Service
        const bus = yield* Bus.Service
        const ready = yield* Deferred.make<void>()
        const session = yield* ssn.create({})
        const msg = yield* createUserMessage(session.id, "hello")
        const msgs = yield* ssn.messages({ sessionID: session.id })
        const off = yield* bus.subscribeCallback(SessionStatus.Event.Status, (evt) => {
          if (evt.properties.sessionID !== session.id) return
          if (evt.properties.status.type !== "retry") return
          Deferred.doneUnsafe(ready, Effect.void)
        })
        yield* Effect.addFinalizer(() => Effect.sync(off))

        const fiber = yield* SessionCompaction.use
          .process({
            parentID: msg.id,
            messages: msgs,
            sessionID: session.id,
            auto: false,
          })
          .pipe(Effect.forkChild)

        yield* Deferred.await(ready).pipe(Effect.timeout("1 second"))
        const start = Date.now()
        yield* Fiber.interrupt(fiber)
        const exit = yield* Fiber.await(fiber).pipe(Effect.timeout("250 millis"))

        expect(Exit.isFailure(exit)).toBe(true)
        if (Exit.isFailure(exit)) {
          expect(Cause.hasInterrupts(exit.cause)).toBe(true)
          expect(Date.now() - start).toBeLessThan(250)
        }
      }).pipe(withCompaction({ llm: stub.layer }))
    },
    { git: true },
  )

  itCompaction.instance(
    "does not leave a summary assistant when aborted before processor setup",
    () =>
      Effect.gen(function* () {
        const ready = yield* Deferred.make<void>()
        return yield* Effect.gen(function* () {
          const ssn = yield* SessionNs.Service
          const session = yield* ssn.create({})
          const msg = yield* createUserMessage(session.id, "hello")
          const msgs = yield* ssn.messages({ sessionID: session.id })
          const fiber = yield* SessionCompaction.use
            .process({
              parentID: msg.id,
              messages: msgs,
              sessionID: session.id,
              auto: false,
            })
            .pipe(Effect.forkChild)

          yield* Deferred.await(ready).pipe(Effect.timeout("1 second"))
          yield* Fiber.interrupt(fiber)
          const exit = yield* Fiber.await(fiber).pipe(Effect.timeout("250 millis"))
          const all = yield* ssn.messages({ sessionID: session.id })

          expect(Exit.isFailure(exit)).toBe(true)
          if (Exit.isFailure(exit)) expect(Cause.hasInterrupts(exit.cause)).toBe(true)
          expect(all.some((msg) => msg.info.role === "assistant" && msg.info.summary)).toBe(false)
        }).pipe(withCompaction({ plugin: plugin(ready) }))
      }),
    { git: true },
  )

  itCompaction.instance(
    "silently drops reasoning-delta arriving without prior reasoning-start",
    () => {
      // Regression: PR initially auto-created a reasoning Part for orphan deltas (no preceding
      // reasoning-start). Reverted to match dev — drop silently. Pinned here so any future
      // change to processor.ts reasoning-delta handling triggers this test.
      const stub = llm()
      stub.push(
        Stream.make(
          LLMEvent.reasoningDelta({ id: "orphan-1", text: "stray reasoning" }),
          LLMEvent.textStart({ id: "txt-0" }),
          LLMEvent.textDelta({ id: "txt-0", text: "summary" }),
          LLMEvent.textEnd({ id: "txt-0" }),
          LLMEvent.stepFinish({ index: 0, reason: "stop", usage: basicUsage() }),
          LLMEvent.finish({ reason: "stop", usage: basicUsage() }),
        ),
      )
      return Effect.gen(function* () {
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        const msg = yield* createUserMessage(session.id, "hello")
        const msgs = yield* ssn.messages({ sessionID: session.id })
        yield* SessionCompaction.use.process({
          parentID: msg.id,
          messages: msgs,
          sessionID: session.id,
          auto: false,
        })

        const summary = (yield* ssn.messages({ sessionID: session.id })).find(
          (item) => item.info.role === "assistant" && item.info.summary,
        )
        expect(summary?.parts.some((part) => part.type === "reasoning")).toBe(false)
        // Sanity: the text part still got through.
        expect(summary?.parts.some((part) => part.type === "text" && part.text === "summary")).toBe(true)
      }).pipe(withCompaction({ llm: stub.layer }))
    },
    { git: true },
  )

  itCompaction.instance(
    "does not allow tool calls while generating the summary",
    () => {
      const stub = llm()
      stub.push(
        Stream.make(
          LLMEvent.toolCall({ id: "call-1", name: "_noop", input: {} }),
          LLMEvent.stepFinish({
            index: 0,
            reason: "tool-calls",
            usage: basicUsage(),
          }),
          LLMEvent.finish({
            reason: "tool-calls",
            usage: basicUsage(),
          }),
        ),
      )
      return Effect.gen(function* () {
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        const msg = yield* createUserMessage(session.id, "hello")
        const msgs = yield* ssn.messages({ sessionID: session.id })
        yield* SessionCompaction.use.process({ parentID: msg.id, messages: msgs, sessionID: session.id, auto: false })

        const summary = (yield* ssn.messages({ sessionID: session.id })).find(
          (item) => item.info.role === "assistant" && item.info.summary,
        )

        expect(summary?.info.role).toBe("assistant")
        expect(summary?.parts.some((part) => part.type === "tool")).toBe(false)
      }).pipe(withCompaction({ llm: stub.layer }))
    },
    { git: true },
  )

  itCompaction.instance(
    "summarizes only the head while keeping recent tail out of summary input",
    () => {
      const stub = llm()
      let captured = ""
      stub.push(
        reply("summary", (input) => {
          captured = JSON.stringify(input.messages)
        }),
      )
      return Effect.gen(function* () {
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        yield* createUserMessage(session.id, "older context")
        yield* createUserMessage(session.id, "keep this turn")
        yield* createUserMessage(session.id, "and this one too")
        yield* createCompactionMarker(session.id)

        const msgs = yield* ssn.messages({ sessionID: session.id })
        const parent = msgs.at(-1)?.info.id
        expect(parent).toBeTruthy()
        yield* SessionCompaction.use.process({
          parentID: parent!,
          messages: msgs,
          sessionID: session.id,
          auto: false,
        })

        expect(captured).toContain("older context")
        expect(captured).not.toContain("keep this turn")
        expect(captured).not.toContain("and this one too")
        expect(captured).not.toContain("What did we do so far?")
      }).pipe(withCompaction({ llm: stub.layer }))
    },
    { git: true },
  )

  itCompaction.instance(
    "anchors repeated compactions with the previous summary",
    () => {
      const stub = llm()
      let captured = ""
      stub.push(reply("summary one"))
      stub.push(
        reply("summary two", (input) => {
          captured = JSON.stringify(input.messages)
        }),
      )

      return Effect.gen(function* () {
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        yield* createUserMessage(session.id, "older context")
        yield* createUserMessage(session.id, "keep this turn")
        yield* createCompactionMarker(session.id)

        let msgs = yield* ssn.messages({ sessionID: session.id })
        let parent = msgs.at(-1)?.info.id
        expect(parent).toBeTruthy()
        yield* SessionCompaction.use.process({ parentID: parent!, messages: msgs, sessionID: session.id, auto: false })

        yield* createUserMessage(session.id, "latest turn")
        yield* createCompactionMarker(session.id)

        msgs = MessageV2.filterCompacted(MessageV2.stream(session.id))
        parent = msgs.at(-1)?.info.id
        expect(parent).toBeTruthy()
        yield* SessionCompaction.use.process({ parentID: parent!, messages: msgs, sessionID: session.id, auto: false })

        expect(captured).toContain("<previous-summary>")
        expect(captured).toContain("summary one")
        expect(captured.match(/summary one/g)?.length).toBe(1)
        expect(captured).toContain("## Constraints & Preferences")
        expect(captured).toContain("## Progress")
      }).pipe(withCompaction({ llm: stub.layer }))
    },
    { git: true },
  )

  itCompaction.instance("keeps recent pre-compaction turns across repeated compactions", () => {
    const stub = llm()
    stub.push(reply("summary one"))
    stub.push(reply("summary two"))

    return Effect.gen(function* () {
      const ssn = yield* SessionNs.Service
      const session = yield* ssn.create({})
      const u1 = yield* createUserMessage(session.id, "one")
      const u2 = yield* createUserMessage(session.id, "two")
      const u3 = yield* createUserMessage(session.id, "three")
      yield* createCompactionMarker(session.id)

      let msgs = yield* ssn.messages({ sessionID: session.id })
      let parent = msgs.at(-1)?.info.id
      expect(parent).toBeTruthy()
      yield* SessionCompaction.use.process({ parentID: parent!, messages: msgs, sessionID: session.id, auto: false })

      const u4 = yield* createUserMessage(session.id, "four")
      yield* createCompactionMarker(session.id)

      msgs = MessageV2.filterCompacted(MessageV2.stream(session.id))
      parent = msgs.at(-1)?.info.id
      expect(parent).toBeTruthy()
      yield* SessionCompaction.use.process({ parentID: parent!, messages: msgs, sessionID: session.id, auto: false })

      const filtered = MessageV2.filterCompacted(MessageV2.stream(session.id))
      const ids = filtered.map((msg) => msg.info.id)

      expect(ids).not.toContain(u1.id)
      expect(ids).not.toContain(u2.id)
      expect(ids).toContain(u3.id)
      expect(ids).toContain(u4.id)
      expect(filtered.some((msg) => msg.info.role === "assistant" && msg.info.summary)).toBe(true)
      expect(
        filtered.some((msg) => msg.info.role === "user" && msg.parts.some((part) => part.type === "compaction")),
      ).toBe(true)
    }).pipe(withCompaction({ llm: stub.layer, config: cfg({ tail_turns: 2, preserve_recent_tokens: 10_000 }) }))
  })

  itCompaction.instance(
    "ignores previous summaries when sizing the retained tail",
    Effect.gen(function* () {
      const ssn = yield* SessionNs.Service
      const test = yield* TestInstance
      const session = yield* ssn.create({})
      yield* createUserMessage(session.id, "older")
      const keep = yield* createUserMessage(session.id, "keep this turn")
      const keepReply = yield* createAssistantMessage(session.id, keep.id, test.directory)
      yield* ssn.updatePart({
        id: PartID.ascending(),
        messageID: keepReply.id,
        sessionID: session.id,
        type: "text",
        text: "keep reply",
      })

      yield* createCompactionMarker(session.id)
      const firstCompaction = (yield* ssn.messages({ sessionID: session.id })).at(-1)?.info.id
      expect(firstCompaction).toBeTruthy()
      yield* createSummaryAssistantMessage(session.id, firstCompaction!, test.directory, "summary ".repeat(800))

      const recent = yield* createUserMessage(session.id, "recent turn")
      const recentReply = yield* createAssistantMessage(session.id, recent.id, test.directory)
      yield* ssn.updatePart({
        id: PartID.ascending(),
        messageID: recentReply.id,
        sessionID: session.id,
        type: "text",
        text: "recent reply",
      })

      yield* createCompactionMarker(session.id)
      const msgs = yield* ssn.messages({ sessionID: session.id })
      const parent = msgs.at(-1)?.info.id
      expect(parent).toBeTruthy()
      yield* SessionCompaction.use.process({ parentID: parent!, messages: msgs, sessionID: session.id, auto: false })

      const part = yield* readCompactionPart(session.id)
      expect(part?.type).toBe("compaction")
      expect(part?.tail_start_id).toBe(keep.id)
    }).pipe(withCompaction({ config: cfg({ tail_turns: 2, preserve_recent_tokens: 500 }) })),
  )
})

// == ALKERA EDIT START: the summariser summarises — it is never handed the request it
// interrupts as a turn to answer, never a data dump to copy, and what it stores is the
// template alone. A fixture chat: an anchor fact, a tool result of 500 CSV rows and an
// assistant dump of 400 rows, then a question only the anchor turn can answer.
const CSV_HEADER = "id,date,region,rep,product,qty,unit,total,note"
function csvRows(count: number, offset = 60_001) {
  return Array.from(
    { length: count },
    (_, i) =>
      `${offset + i},2026-07-${String((i % 28) + 1).padStart(2, "0")},REGION-${i % 7},REP-${i % 15},PRODUCT-${i % 10},${100 + i},34.50,${(100 + i) * 34.5},"Note ${i} padded to exactly forty five characters"`,
  )
}
function csvBlock(count: number, offset?: number) {
  return [CSV_HEADER, ...csvRows(count, offset)].join("\n")
}
const ANCHOR = "Remember: depot PELICAN-7741 holds 8312 pallets and ships on lane ALPHA-9."
const QUESTION = "Which shipping lane does PELICAN-7741 use, and how many pallets does it hold?"

function createAssistantWithParts(
  sessionID: SessionID,
  parentID: MessageID,
  root: string,
  input: { text?: string; toolOutput?: string; finish?: string },
) {
  return SessionNs.Service.use((ssn) =>
    Effect.gen(function* () {
      const msg = yield* ssn.updateMessage({
        id: MessageID.ascending(),
        role: "assistant",
        sessionID,
        mode: "build",
        agent: "build",
        path: { cwd: root, root },
        cost: 0,
        tokens: { output: 0, input: 0, reasoning: 0, cache: { read: 0, write: 0 } },
        modelID: ref.modelID,
        providerID: ref.providerID,
        parentID,
        time: { created: Date.now() },
        ...(input.finish ? { finish: input.finish } : {}),
      })
      if (input.toolOutput !== undefined) {
        yield* ssn.updatePart({
          id: PartID.ascending(),
          messageID: msg.id,
          sessionID,
          type: "tool",
          callID: "call-1",
          tool: "read",
          state: {
            status: "completed",
            input: { filePath: "/work/sales.csv" },
            output: input.toolOutput,
            title: "sales.csv",
            metadata: {},
            time: { start: 1, end: 2 },
          },
        })
      }
      if (input.text !== undefined) {
        yield* ssn.updatePart({
          id: PartID.ascending(),
          messageID: msg.id,
          sessionID,
          type: "text",
          text: input.text,
          time: { start: 1, end: 2 },
        })
      }
      return msg
    }),
  )
}

/** The text of every message the summariser was handed, by role, in order. */
function texts(input: LLM.StreamInput) {
  return input.messages.map((m) => {
    const content = m.content
    const text =
      typeof content === "string"
        ? content
        : (content as Array<{ type: string; text?: string; output?: unknown }>)
            .map((p) => (p.type === "text" ? (p.text ?? "") : p.type === "tool-result" ? JSON.stringify(p.output) : ""))
            .join("\n")
    return { role: m.role, text }
  })
}

function longestDataRun(text: string) {
  let best = 0
  let run = 0
  for (const line of text.split("\n")) {
    if (/^\d{5},\d{4}-\d{2}-\d{2},REGION-\d/.test(line.trim())) run++
    else run = 0
    best = Math.max(best, run)
  }
  return best
}

describe("session.compaction.summariser", () => {
  itCompaction.instance(
    "the request that overran the window is quoted to the summariser as context and re-attached after the summary",
    () => {
      const stub = llm()
      let seen: LLM.StreamInput | undefined
      stub.push(
        reply("## Goal\n- Depot report\n\n## Relevant Files\n- (none)", (input) => {
          seen = input
        }),
      )
      return Effect.gen(function* () {
        const test = yield* TestInstance
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        const u1 = yield* createUserMessage(session.id, ANCHOR)
        yield* createAssistantWithParts(session.id, u1.id, test.directory, { text: "Noted.", finish: "end_turn" })
        const u2 = yield* createUserMessage(session.id, "Read /work/sales.csv and print every row.")
        yield* createAssistantWithParts(session.id, u2.id, test.directory, {
          toolOutput: csvBlock(500),
          text: csvBlock(400),
          finish: "end_turn",
        })
        // The interrupted turn: the question, and a reply that is still streaming rows
        // when the window fills — too large for the retained tail, so upstream folded
        // the question into the summariser's input as a turn to answer.
        const u3 = yield* createUserMessage(session.id, QUESTION)
        yield* createAssistantWithParts(session.id, u3.id, test.directory, { text: csvBlock(400, 70_001) })
        yield* createCompactionMarker(session.id)

        const msgs = yield* ssn.messages({ sessionID: session.id })
        const parent = msgs.at(-1)!.info.id
        const result = yield* SessionCompaction.use.process({
          parentID: parent,
          messages: msgs,
          sessionID: session.id,
          auto: true,
        })
        expect(result).toBe("continue")
        expect(seen).toBeDefined()
        const handed = texts(seen!)

        // The question reaches the summariser only inside the instruction's quoted block,
        // and the instruction is the LAST thing the summariser reads — the position a
        // model answers from. Anything else last is a request it would answer instead.
        const asTurn = handed.filter((m) => m.text.includes(QUESTION))
        expect(asTurn).toHaveLength(1)
        expect(asTurn[0]!.text).toContain("<in-progress-request>")
        expect(asTurn[0]!.text).toContain("single-sentence task summary")
        expect(handed.at(-1)).toMatchObject({ role: "user" })
        expect(handed.at(-1)!.text).toContain("Keep the whole summary under")
        expect(handed.indexOf(asTurn[0]!)).toBe(handed.length - 1)

        // The interrupted request keeps its place and its user role, standing in for its
        // own text: dropping the message would open the call with its assistant replies,
        // which a provider that requires a leading user turn refuses.
        expect(handed[0]).toMatchObject({ role: "user" })
        const placeholder = handed.filter((m) => m.text.includes("The request being handled when the history"))
        expect(placeholder).toHaveLength(1)
        expect(placeholder[0]!.role).toBe("user")

        // (The compaction agent's system prompt is attached by the real LLM service,
        // which this fake replaces; the mock-e2e in apps/cli asserts it on the wire.)

        // The anchor facts are in what it was handed; the dumps are not.
        const all = handed.map((m) => m.text).join("\n")
        expect(all).toContain("PELICAN-7741")
        expect(all).toContain("8312")
        expect(all).toContain("not shown to the summarizer")
        expect(all).toContain("Tool output truncated for compaction")
        expect(all).not.toContain("60201,")
        expect(all).not.toContain("70201,")
        // A 3 000-char text cap and upstream's 2 000-char tool cap each leave a couple of
        // dozen rows in view, never the 400- and 500-row dumps.
        expect(longestDataRun(all)).toBeLessThan(40)

        // Nothing of the interrupted turn survived the boundary, so the question itself
        // is the message after the summary — not a "continue" nudge.
        const after = yield* ssn.messages({ sessionID: session.id })
        const last = after.at(-1)!
        expect(last.info.role).toBe("user")
        expect(last.parts.find((p) => p.type === "text")).toMatchObject({ type: "text", text: QUESTION })
        expect(
          after.some((m) => m.parts.some((p) => p.type === "text" && p.metadata?.compaction_continue === true)),
        ).toBe(false)
        const marker = after.find((m) => m.parts.some((p) => p.type === "compaction"))!
        const part = marker.parts.find((p): p is MessageV2.CompactionPart => p.type === "compaction")!
        expect(part.tail_start_id).toBeUndefined()
      }).pipe(withCompaction({ llm: stub.layer }))
    },
    { git: true },
  )

  itCompaction.instance(
    "a request the tail retains is kept out of the summariser and followed by a nudge that forbids asking the user",
    () => {
      const stub = llm()
      let seen: LLM.StreamInput | undefined
      stub.push(
        reply("## Goal\n- Depot report\n\n## Relevant Files\n- (none)", (input) => {
          seen = input
        }),
      )
      return Effect.gen(function* () {
        const test = yield* TestInstance
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        const u1 = yield* createUserMessage(session.id, ANCHOR)
        yield* createAssistantWithParts(session.id, u1.id, test.directory, { text: "Noted.", finish: "end_turn" })
        const u2 = yield* createUserMessage(session.id, "Read /work/sales.csv and print every row.")
        yield* createAssistantWithParts(session.id, u2.id, test.directory, { text: csvBlock(400), finish: "end_turn" })
        const u3 = yield* createUserMessage(session.id, QUESTION)
        yield* createCompactionMarker(session.id)

        const msgs = yield* ssn.messages({ sessionID: session.id })
        const result = yield* SessionCompaction.use.process({
          parentID: msgs.at(-1)!.info.id,
          messages: msgs,
          sessionID: session.id,
          auto: true,
        })
        expect(result).toBe("continue")
        const all = texts(seen!)
          .map((m) => m.text)
          .join("\n")
        expect(all).not.toContain(QUESTION)
        expect(all).not.toContain("<in-progress-request>")

        const after = yield* ssn.messages({ sessionID: session.id })
        const marker = after.find((m) => m.parts.some((p) => p.type === "compaction"))!
        const part = marker.parts.find((p): p is MessageV2.CompactionPart => p.type === "compaction")!
        expect(part.tail_start_id).toBe(u3.id)
        const last = after.at(-1)!
        const nudge = last.parts.find((p) => p.type === "text")
        expect(nudge).toMatchObject({ type: "text", synthetic: true, metadata: { compaction_continue: true } })
        if (nudge?.type === "text") {
          expect(nudge.text).toContain("Do not ask what to do next")
          expect(nudge.text).not.toContain("<in-progress-request>")
          expect(nudge.text).not.toContain("stop and ask for clarification")
        }
      }).pipe(withCompaction({ llm: stub.layer }))
    },
    { git: true },
  )

  itCompaction.instance(
    "what is stored is the template alone: no thinking aloud, no copied rows, no closing question",
    () => {
      const stub = llm()
      const raw = [
        "The user is asking me to create an anchored summary from the conversation history above. Let me review what happened.",
        "## Goal\n- Build the depot report for PELICAN-7741 (8312 pallets, lane ALPHA-9)",
        "## Constraints & Preferences\n- (none)",
        "## Progress\n### Done\n- Read /work/sales.csv\n### In Progress\n- (none)\n### Blocked\n- (none)",
        "## Key Decisions\n- (none)",
        "## Next Steps\n- (none)",
        `## Critical Context\n- The first rows of /work/sales.csv:\n${csvRows(14).join("\n")}`,
        "## Relevant Files\n- /work/sales.csv: the sales dump",
        "I've completed the work outlined in our previous conversation. How would you like to proceed?",
      ].join("\n\n")
      stub.push(reply(raw))
      return Effect.gen(function* () {
        const test = yield* TestInstance
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        const u1 = yield* createUserMessage(session.id, ANCHOR)
        yield* createAssistantWithParts(session.id, u1.id, test.directory, { text: "Noted.", finish: "end_turn" })
        const u2 = yield* createUserMessage(session.id, "Read /work/sales.csv and print every row.")
        yield* createAssistantWithParts(session.id, u2.id, test.directory, { text: csvBlock(50), finish: "end_turn" })
        yield* createUserMessage(session.id, QUESTION)
        yield* createCompactionMarker(session.id)

        const msgs = yield* ssn.messages({ sessionID: session.id })
        const result = yield* SessionCompaction.use.process({
          parentID: msgs.at(-1)!.info.id,
          messages: msgs,
          sessionID: session.id,
          auto: false,
        })
        expect(result).toBe("continue")
        const after = yield* ssn.messages({ sessionID: session.id })
        const summary = after.find((m) => m.info.role === "assistant" && m.info.summary)!
        const stored = summary.parts
          .filter((p): p is MessageV2.TextPart => p.type === "text")
          .map((p) => p.text.trim())
          .filter(Boolean)
          .join("\n\n")
        expect(stored.startsWith("## Goal")).toBe(true)
        expect(stored).toContain("PELICAN-7741")
        expect(stored).toContain("8312")
        expect(stored).toContain("/work/sales.csv: the sales dump")
        expect(stored).not.toContain("Let me review")
        expect(stored).not.toContain("How would you like to proceed")
        expect(stored.endsWith("?")).toBe(false)
        expect(longestDataRun(stored)).toBeLessThanOrEqual(3)
        expect(stored).toContain("11 more rows not kept in the summary")
      }).pipe(withCompaction({ llm: stub.layer }))
    },
    { git: true },
  )

  const template = [
    "## Goal\n- Ship the report",
    "## Constraints & Preferences\n- (none)",
    "## Progress\n### Done\n- (none)\n### In Progress\n- (none)\n### Blocked\n- (none)",
    "## Key Decisions\n- (none)",
    "## Next Steps\n- Should the report include Q3? (open)",
    "## Critical Context\n- (none)",
    "## Relevant Files\n- (none)",
  ].join("\n\n")

  test.each([
    ["a clean summary is unchanged", template, template],
    [
      "thinking aloud before the first heading is cut",
      `Let me review the history.\n\nSo far:\n\n${template}`,
      template,
    ],
    ["a closing question to the reader is cut", `${template}\n\nHow would you like to proceed?`, template],
    [
      "a closing offer to the reader is cut",
      `${template}\n\nLet me know if you want me to continue with the export.`,
      template,
    ],
    [
      "two closing paragraphs are cut",
      `${template}\n\nI have finished summarizing.\n\nWhat would you like next?`,
      `${template}\n\nI have finished summarizing.`,
    ],
    ["a bullet that ends in a question mark is a real open question and stays", template, template],
    [
      "a short table stays",
      `${template}\n\n| a | b |\n|---|---|\n| 1 | 2 |`,
      `${template}\n\n| a | b |\n|---|---|\n| 1 | 2 |`,
    ],
    [
      "prose with commas is not a data row",
      `${template}\n\n- apps/cli/x.py: parses the config, validates it, joins the rows, writes the manifest`,
      `${template}\n\n- apps/cli/x.py: parses the config, validates it, joins the rows, writes the manifest`,
    ],
    ["text without the template heading is only trimmed", "  plain summary  ", "plain summary"],
  ])("cleanSummaryText: %s", (_name, input, expected) => {
    expect(SessionCompaction.cleanSummaryText(input)).toBe(expected)
  })

  test("cleanSummaryText collapses a long run of CSV rows and keeps a run at the limit", () => {
    const rows = csvRows(12).join("\n")
    const cleaned = SessionCompaction.cleanSummaryText(`## Goal\n- x\n\n## Critical Context\n${rows}`)
    expect(longestDataRun(cleaned)).toBe(3)
    expect(cleaned).toContain("[… 9 more rows not kept in the summary; see the source named above]")
    const atLimit = `## Goal\n- x\n\n## Critical Context\n${csvRows(10).join("\n")}`
    expect(SessionCompaction.cleanSummaryText(atLimit)).toBe(atLimit)
  })

  test.each([
    ["a tiny window still gets the floor", 2_000, 400],
    ["a mid window gets its share", 20_000, 769],
    ["a huge window is capped", 880_000, 2_500],
  ])("summaryBudgetWords: %s", (_name, usable, words) => {
    expect(SessionCompaction.summaryBudgetWords(usable)).toBe(words)
  })

  test("summaryMaxChars leaves a budgeted summary room and still bounds a runaway one", () => {
    // The floor budget already allows several times the template's own size, and the
    // ceiling for the widest window is far under the 36 KB summary that started this.
    expect(SessionCompaction.summaryMaxChars(400)).toBeGreaterThan(template.length * 3)
    expect(SessionCompaction.summaryMaxChars(2_500)).toBeLessThan(36_000)
  })

  const bullets = (count: number) =>
    `## Goal\n${Array.from({ length: count }, (_, i) => `- fact ${i} about the depot report`).join("\n")}`

  test.each([
    ["exactly at the ceiling, nothing is cut", 0],
    ["one character under the ceiling, nothing is cut", 1],
  ])("cleanSummaryText: %s", (_name, slack) => {
    const text = bullets(200)
    expect(SessionCompaction.cleanSummaryText(text, { maxChars: text.length + slack })).toBe(text)
  })

  test("cleanSummaryText cuts a runaway summary on a line boundary and says how much went", () => {
    const text = bullets(4_000)
    expect(text.length).toBeGreaterThan(50_000)
    const bounded = SessionCompaction.cleanSummaryText(text, { maxChars: 5_000 })
    expect(bounded.length).toBeLessThan(5_200)
    expect(bounded.startsWith("## Goal\n- fact 0 about the depot report")).toBe(true)
    // Cut between bullets, never mid-bullet, and the reader is told what is missing.
    const kept = bounded.split("\n").filter((line) => line.startsWith("- fact"))
    expect(kept.length).toBeGreaterThan(100)
    expect(kept.every((line) => /^- fact \d+ about the depot report$/.test(line))).toBe(true)
    expect(bounded).toMatch(/\[… \d+ characters over the summary's length budget were dropped;/)
    expect(bounded).toContain("still on the transcript")
  })

  itCompaction.instance(
    "a summary that ignores its word budget is stored bounded, not whole",
    () => {
      const stub = llm()
      // Prose, not rows: the data-run collapse cannot touch it, so only the ceiling can.
      const runaway = `## Goal\n- Depot report\n\n## Critical Context\n${Array.from(
        { length: 3_000 },
        (_, i) => `- observation ${i} recorded while reading the sales dump for PELICAN-7741`,
      ).join("\n")}`
      stub.push(reply(runaway))
      return Effect.gen(function* () {
        const test = yield* TestInstance
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        const u1 = yield* createUserMessage(session.id, ANCHOR)
        yield* createAssistantWithParts(session.id, u1.id, test.directory, { text: "Noted.", finish: "end_turn" })
        yield* createCompactionMarker(session.id)

        const msgs = yield* ssn.messages({ sessionID: session.id })
        expect(
          yield* SessionCompaction.use.process({
            parentID: msgs.at(-1)!.info.id,
            messages: msgs,
            sessionID: session.id,
            auto: false,
          }),
        ).toBe("continue")

        const after = yield* ssn.messages({ sessionID: session.id })
        const stored = after
          .find((m) => m.info.role === "assistant" && m.info.summary)!
          .parts.filter((p): p is MessageV2.TextPart => p.type === "text")
          .map((p) => p.text.trim())
          .filter(Boolean)
          .join("\n\n")
        expect(runaway.length).toBeGreaterThan(200_000)
        // The widest ceiling this model's window can produce, and well under the raw text.
        expect(stored.length).toBeLessThanOrEqual(SessionCompaction.summaryMaxChars(2_500) + 200)
        expect(stored.startsWith("## Goal")).toBe(true)
        expect(stored).toContain("observation 0 recorded")
        expect(stored).toContain("characters over the summary's length budget were dropped")
      }).pipe(withCompaction({ llm: stub.layer }))
    },
    { git: true },
  )

  itCompaction.instance(
    "a summary that does not follow the template is stored as the model wrote it",
    () => {
      const stub = llm()
      // No template heading, and the whole thing addresses the reader: every rule that
      // could fire, fires — and a chat with an odd summary still beats one with none.
      stub.push(reply("Would you like me to summarize the conversation?"))
      return Effect.gen(function* () {
        const test = yield* TestInstance
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        const u1 = yield* createUserMessage(session.id, ANCHOR)
        yield* createAssistantWithParts(session.id, u1.id, test.directory, { text: "Noted.", finish: "end_turn" })
        yield* createCompactionMarker(session.id)

        const msgs = yield* ssn.messages({ sessionID: session.id })
        yield* SessionCompaction.use.process({
          parentID: msgs.at(-1)!.info.id,
          messages: msgs,
          sessionID: session.id,
          auto: false,
        })

        const after = yield* ssn.messages({ sessionID: session.id })
        const stored = after
          .find((m) => m.info.role === "assistant" && m.info.summary)!
          .parts.filter((p): p is MessageV2.TextPart => p.type === "text")
          .map((p) => p.text)
          .join("")
        expect(stored).toBe("Would you like me to summarize the conversation?")
      }).pipe(withCompaction({ llm: stub.layer }))
    },
    { git: true },
  )

  itCompaction.instance(
    "a compaction with nothing in progress keeps upstream's own continue nudge",
    () => {
      const stub = llm()
      stub.push(reply("## Goal\n- Depot report\n\n## Relevant Files\n- (none)"))
      return Effect.gen(function* () {
        const test = yield* TestInstance
        const ssn = yield* SessionNs.Service
        const session = yield* ssn.create({})
        // Every turn's reply finished, so there is no interrupted request to name.
        for (const text of [ANCHOR, "Read /work/sales.csv and print every row.", QUESTION]) {
          const user = yield* createUserMessage(session.id, text)
          yield* createAssistantWithParts(session.id, user.id, test.directory, {
            text: csvBlock(200),
            finish: "end_turn",
          })
        }
        yield* createCompactionMarker(session.id)

        const msgs = yield* ssn.messages({ sessionID: session.id })
        yield* SessionCompaction.use.process({
          parentID: msgs.at(-1)!.info.id,
          messages: msgs,
          sessionID: session.id,
          auto: true,
        })

        const after = yield* ssn.messages({ sessionID: session.id })
        const nudge = after.at(-1)!.parts.find((p) => p.type === "text")
        expect(nudge).toMatchObject({ type: "text", metadata: { compaction_continue: true } })
        if (nudge?.type === "text") {
          expect(nudge.text).toBe(
            "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.",
          )
        }
      }).pipe(withCompaction({ llm: stub.layer }))
    },
    { git: true },
  )

  itCompaction.instance(
    "pendingTurn: only a user message whose reply has not finished is pending",
    Effect.gen(function* () {
      const test = yield* TestInstance
      const ssn = yield* SessionNs.Service
      const session = yield* ssn.create({})
      const read = () => ssn.messages({ sessionID: session.id })
      expect(SessionCompaction.pendingTurn(yield* read())).toBeUndefined()

      const u1 = yield* createUserMessage(session.id, "one")
      expect(SessionCompaction.pendingTurn(yield* read())?.message.info.id).toBe(u1.id)

      yield* createAssistantWithParts(session.id, u1.id, test.directory, { text: "…", finish: "tool-calls" })
      expect(SessionCompaction.pendingTurn(yield* read())?.message.info.id).toBe(u1.id)

      yield* createAssistantWithParts(session.id, u1.id, test.directory, { text: "done", finish: "end_turn" })
      expect(SessionCompaction.pendingTurn(yield* read())).toBeUndefined()

      const u2 = yield* createUserMessage(session.id, "two")
      const errored = yield* createAssistantWithParts(session.id, u2.id, test.directory, { finish: "error" })
      yield* ssn.updateMessage({
        ...errored,
        error: new MessageV2.ContextOverflowError({ message: "boom" }).toObject(),
      })
      expect(SessionCompaction.pendingTurn(yield* read())?.message.info.id).toBe(u2.id)

      // The boundary marker itself is never the pending turn.
      yield* createCompactionMarker(session.id)
      expect(SessionCompaction.pendingTurn(yield* read())?.message.info.id).toBe(u2.id)
    }),
  )
})
// == ALKERA EDIT END

describe("util.token.estimate", () => {
  test("estimates tokens from text (4 chars per token)", () => {
    const text = "x".repeat(4000)
    expect(Token.estimate(text)).toBe(1000)
  })

  test("estimates tokens from larger text", () => {
    const text = "y".repeat(20_000)
    expect(Token.estimate(text)).toBe(5000)
  })

  test("returns 0 for empty string", () => {
    expect(Token.estimate("")).toBe(0)
  })
})

describe("SessionNs.getUsage", () => {
  test("normalizes standard usage to token format", () => {
    const model = createModel({ context: 100_000, output: 32_000 })
    const result = SessionNs.getUsage({
      model,
      usage: usage({ inputTokens: 1000, outputTokens: 500, totalTokens: 1500 }),
    })

    expect(result.tokens.input).toBe(1000)
    expect(result.tokens.output).toBe(500)
    expect(result.tokens.reasoning).toBe(0)
    expect(result.tokens.cache.read).toBe(0)
    expect(result.tokens.cache.write).toBe(0)
  })

  test("extracts cached tokens to cache.read", () => {
    const model = createModel({ context: 100_000, output: 32_000 })
    const result = SessionNs.getUsage({
      model,
      usage: usage({ inputTokens: 1000, outputTokens: 500, totalTokens: 1500, cacheReadInputTokens: 200 }),
    })

    expect(result.tokens.input).toBe(800)
    expect(result.tokens.cache.read).toBe(200)
  })

  test("handles anthropic cache write metadata", () => {
    const model = createModel({ context: 100_000, output: 32_000 })
    const result = SessionNs.getUsage({
      model,
      usage: usage({ inputTokens: 1000, outputTokens: 500, totalTokens: 1500 }),
      metadata: {
        anthropic: {
          cacheCreationInputTokens: 300,
        },
      },
    })

    expect(result.tokens.cache.write).toBe(300)
  })

  test("subtracts cached tokens for anthropic provider", () => {
    const model = createModel({ context: 100_000, output: 32_000 })
    // AI SDK v6 normalizes inputTokens to include cached tokens for all providers
    const result = SessionNs.getUsage({
      model,
      usage: usage({ inputTokens: 1000, outputTokens: 500, totalTokens: 1500, cacheReadInputTokens: 200 }),
      metadata: {
        anthropic: {},
      },
    })

    expect(result.tokens.input).toBe(800)
    expect(result.tokens.cache.read).toBe(200)
  })

  test("separates reasoning tokens from output tokens", () => {
    const model = createModel({ context: 100_000, output: 32_000 })
    const result = SessionNs.getUsage({
      model,
      usage: usage({ inputTokens: 1000, outputTokens: 500, reasoningTokens: 100, totalTokens: 1500 }),
    })

    expect(result.tokens.input).toBe(1000)
    expect(result.tokens.output).toBe(400)
    expect(result.tokens.reasoning).toBe(100)
    expect(result.tokens.total).toBe(1500)
  })

  test("does not double count reasoning tokens in cost", () => {
    const model = createModel({
      context: 100_000,
      output: 32_000,
      cost: {
        input: 0,
        output: 15,
        cache: { read: 0, write: 0 },
      },
    })
    const result = SessionNs.getUsage({
      model,
      usage: usage({ inputTokens: 0, outputTokens: 1_000_000, reasoningTokens: 250_000, totalTokens: 1_000_000 }),
    })

    expect(result.tokens.output).toBe(750_000)
    expect(result.tokens.reasoning).toBe(250_000)
    expect(result.cost).toBe(15)
  })

  test("handles undefined optional values gracefully", () => {
    const model = createModel({ context: 100_000, output: 32_000 })
    const result = SessionNs.getUsage({
      model,
      usage: usage({ inputTokens: 0, outputTokens: 0, totalTokens: 0 }),
    })

    expect(result.tokens.input).toBe(0)
    expect(result.tokens.output).toBe(0)
    expect(result.tokens.reasoning).toBe(0)
    expect(result.tokens.cache.read).toBe(0)
    expect(result.tokens.cache.write).toBe(0)
    expect(Number.isNaN(result.cost)).toBe(false)
  })

  test("calculates cost correctly", () => {
    const model = createModel({
      context: 100_000,
      output: 32_000,
      cost: {
        input: 3,
        output: 15,
        cache: { read: 0.3, write: 3.75 },
      },
    })
    const result = SessionNs.getUsage({
      model,
      usage: usage({ inputTokens: 1_000_000, outputTokens: 100_000, totalTokens: 1_100_000 }),
    })

    expect(result.cost).toBe(3 + 1.5)
  })

  test("uses matching context cost tier before over-200k fallback", () => {
    const model = createModel({
      context: 1_000_000,
      output: 32_000,
      cost: {
        input: 1,
        output: 2,
        cache: { read: 0.1, write: 0.5 },
        tiers: [
          {
            input: 3,
            output: 4,
            cache: { read: 0.3, write: 1.5 },
            tier: { type: "context", size: 200_000 },
          },
          {
            input: 5,
            output: 6,
            cache: { read: 0.5, write: 2.5 },
            tier: { type: "context", size: 500_000 },
          },
        ],
        experimentalOver200K: {
          input: 100,
          output: 100,
          cache: { read: 100, write: 100 },
        },
      },
    })
    const result = SessionNs.getUsage({
      model,
      usage: usage({
        inputTokens: 650_000,
        outputTokens: 100_000,
        totalTokens: 750_000,
        cacheReadInputTokens: 100_000,
      }),
    })

    expect(result.tokens.input).toBe(550_000)
    expect(result.cost).toBe(2.75 + 0.6 + 0.05)
  })

  test("falls back to over-200k pricing when no cost tier matches", () => {
    const model = createModel({
      context: 1_000_000,
      output: 32_000,
      cost: {
        input: 1,
        output: 2,
        cache: { read: 0.1, write: 0.5 },
        tiers: [
          {
            input: 5,
            output: 6,
            cache: { read: 0.5, write: 2.5 },
            tier: { type: "context", size: 500_000 },
          },
        ],
        experimentalOver200K: {
          input: 3,
          output: 4,
          cache: { read: 0.3, write: 1.5 },
        },
      },
    })
    const result = SessionNs.getUsage({
      model,
      usage: usage({ inputTokens: 300_000, outputTokens: 100_000, totalTokens: 400_000 }),
    })

    expect(result.cost).toBe(0.9 + 0.4)
  })

  test.each(["@ai-sdk/anthropic", "@ai-sdk/amazon-bedrock", "@ai-sdk/google-vertex/anthropic"])(
    "computes total from components for %s models",
    (npm) => {
      const model = createModel({ context: 100_000, output: 32_000, npm })
      // AI SDK v6: inputTokens includes cached tokens for all providers
      const item = usage({
        inputTokens: 1000,
        outputTokens: 500,
        totalTokens: 1500,
        cacheReadInputTokens: 200,
      })
      if (npm === "@ai-sdk/amazon-bedrock") {
        const result = SessionNs.getUsage({
          model,
          usage: item,
          metadata: {
            bedrock: {
              usage: {
                cacheWriteInputTokens: 300,
              },
            },
          },
        })

        // inputTokens (1000) includes cache, so adjusted = 1000 - 200 - 300 = 500
        expect(result.tokens.input).toBe(500)
        expect(result.tokens.cache.read).toBe(200)
        expect(result.tokens.cache.write).toBe(300)
        // total = adjusted (500) + output (500) + cacheRead (200) + cacheWrite (300)
        expect(result.tokens.total).toBe(1500)
        return
      }

      const result = SessionNs.getUsage({
        model,
        usage: item,
        metadata: {
          anthropic: {
            cacheCreationInputTokens: 300,
          },
        },
      })

      // inputTokens (1000) includes cache, so adjusted = 1000 - 200 - 300 = 500
      expect(result.tokens.input).toBe(500)
      expect(result.tokens.cache.read).toBe(200)
      expect(result.tokens.cache.write).toBe(300)
      // total = adjusted (500) + output (500) + cacheRead (200) + cacheWrite (300)
      expect(result.tokens.total).toBe(1500)
    },
  )

  test("extracts cache write tokens from vertex metadata key", () => {
    const model = createModel({ context: 100_000, output: 32_000, npm: "@ai-sdk/google-vertex/anthropic" })
    const result = SessionNs.getUsage({
      model,
      usage: usage({ inputTokens: 1000, outputTokens: 500, totalTokens: 1500, cacheReadInputTokens: 200 }),
      metadata: {
        vertex: {
          cacheCreationInputTokens: 300,
        },
      },
    })

    expect(result.tokens.input).toBe(500)
    expect(result.tokens.cache.read).toBe(200)
    expect(result.tokens.cache.write).toBe(300)
  })
})
