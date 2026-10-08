import { Config, ConfigProvider, Context, Effect, Layer, Option } from "effect"
import { ConfigService } from "@/effect/config-service"

const bool = (name: string) => Config.boolean(name).pipe(Config.withDefault(false))
const positiveInteger = (name: string) =>
  Config.number(name).pipe(
    Config.map((value) => (Number.isInteger(value) && value > 0 ? value : undefined)),
    Config.orElse(() => Config.succeed(undefined)),
  )
const experimental = bool("OPENCODE_EXPERIMENTAL")
const enabledByExperimental = (name: string) =>
  Config.all({ experimental, enabled: Config.boolean(name).pipe(Config.option) }).pipe(
    Config.map((flags) => Option.getOrElse(flags.enabled, () => flags.experimental)),
  )

export class Service extends ConfigService.Service<Service>()("@opencode/RuntimeFlags", {
  autoShare: bool("OPENCODE_AUTO_SHARE"),
  // == ALKERA EDIT START — product setting: ALKERA_* env name (property key unchanged)
  pure: bool("ALKERA_PURE"),
  // == ALKERA EDIT END
  disableDefaultPlugins: bool("OPENCODE_DISABLE_DEFAULT_PLUGINS"),
  // == ALKERA EDIT START — product setting: ALKERA_* env name (property key unchanged)
  disableChannelDb: bool("ALKERA_DISABLE_CHANNEL_DB"),
  // == ALKERA EDIT END
  // == ALKERA EDIT START
  // The parent process hosts the shell tool itself (loopback MCP `alkera_bash`).
  // When set, the native ShellTool is dropped from the model's tool set and our
  // MCP tool is advertised under the bare name `bash` — so the model sees exactly
  // one shell tool, under the name it was pretrained on. The two effects are
  // coupled: dropping the native is what frees the `bash` id for ours.
  parentShell: bool("ALKERA_PARENT_SHELL"),
  // The parent process hosts the fetch tool itself (loopback MCP `web_fetch`).
  // When set, the native WebFetchTool is dropped from the model's tool set, so
  // the model sees exactly one fetch tool instead of two — and never the one the
  // parent's permission policy refuses outright. Independent of parentShell: a
  // deployment can host the shell without the web tools and vice versa.
  parentWebFetch: bool("ALKERA_PARENT_WEB_FETCH"),
  // Whether this deployment serves delegation at all. False drops every
  // subagent-spawning tool (`task`) from the model's tool set — removal, not a
  // permission rule, so nothing downstream can advertise it back. Defaults to
  // true: a box that says nothing keeps the vendor behaviour.
  subagents: Config.boolean("ALKERA_SUBAGENTS_ENABLED").pipe(Config.withDefault(true)),
  // == ALKERA EDIT END
  disableEmbeddedWebUi: bool("OPENCODE_DISABLE_EMBEDDED_WEB_UI"),
  disableExternalSkills: bool("OPENCODE_DISABLE_EXTERNAL_SKILLS"),
  // == ALKERA EDIT START — product setting: ALKERA_* env name (property key unchanged)
  disableLspDownload: bool("ALKERA_DISABLE_LSP_DOWNLOAD"),
  // == ALKERA EDIT END
  skipMigrations: bool("OPENCODE_SKIP_MIGRATIONS"),
  disableClaudeCodePrompt: Config.all({
    broad: bool("OPENCODE_DISABLE_CLAUDE_CODE"),
    direct: bool("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"),
  }).pipe(Config.map((flags) => flags.broad || flags.direct)),
  disableClaudeCodeSkills: Config.all({
    broad: bool("OPENCODE_DISABLE_CLAUDE_CODE"),
    direct: bool("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"),
  }).pipe(Config.map((flags) => flags.broad || flags.direct)),
  enableExa: Config.all({
    experimental,
    enabled: bool("OPENCODE_ENABLE_EXA"),
    legacy: bool("OPENCODE_EXPERIMENTAL_EXA"),
  }).pipe(Config.map((flags) => flags.experimental || flags.enabled || flags.legacy)),
  enableParallel: Config.all({
    enabled: bool("OPENCODE_ENABLE_PARALLEL"),
    legacy: bool("OPENCODE_EXPERIMENTAL_PARALLEL"),
  }).pipe(Config.map((flags) => flags.enabled || flags.legacy)),
  enableExperimentalModels: bool("OPENCODE_ENABLE_EXPERIMENTAL_MODELS"),
  enableQuestionTool: bool("OPENCODE_ENABLE_QUESTION_TOOL"),
  experimentalScout: enabledByExperimental("OPENCODE_EXPERIMENTAL_SCOUT"),
  experimentalBackgroundSubagents: enabledByExperimental("OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS"),
  experimentalLspTy: bool("OPENCODE_EXPERIMENTAL_LSP_TY"),
  experimentalLspTool: enabledByExperimental("OPENCODE_EXPERIMENTAL_LSP_TOOL"),
  experimentalOxfmt: enabledByExperimental("OPENCODE_EXPERIMENTAL_OXFMT"),
  experimentalPlanMode: enabledByExperimental("OPENCODE_EXPERIMENTAL_PLAN_MODE"),
  experimentalEventSystem: enabledByExperimental("OPENCODE_EXPERIMENTAL_EVENT_SYSTEM"),
  experimentalWorkspaces: enabledByExperimental("OPENCODE_EXPERIMENTAL_WORKSPACES"),
  experimentalIconDiscovery: enabledByExperimental("OPENCODE_EXPERIMENTAL_ICON_DISCOVERY"),
  acpNext: bool("OPENCODE_ACP_NEXT"),
  outputTokenMax: positiveInteger("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"),
  // == ALKERA EDIT START — product setting: ALKERA_* env name (property key unchanged)
  bashDefaultTimeoutMs: positiveInteger("ALKERA_BASH_DEFAULT_TIMEOUT_MS"),
  // == ALKERA EDIT END
  experimentalNativeLlm: bool("OPENCODE_EXPERIMENTAL_NATIVE_LLM"),
  client: Config.string("OPENCODE_CLIENT").pipe(Config.withDefault("cli")),
}) {}

export type Info = Context.Service.Shape<typeof Service>

const emptyConfigLayer = Service.defaultLayer.pipe(
  Layer.provide(ConfigProvider.layer(ConfigProvider.fromUnknown({}))),
  Layer.orDie,
)

export const layer = (overrides: Partial<Info> = {}) =>
  Layer.effect(
    Service,
    Effect.gen(function* () {
      const flags = yield* Service
      return Service.of({ ...flags, ...overrides })
    }),
  ).pipe(Layer.provide(emptyConfigLayer))

export const defaultLayer = Service.defaultLayer.pipe(Layer.orDie)

export * as RuntimeFlags from "./runtime-flags"
