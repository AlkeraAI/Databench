import { Config } from "effect"
// == ALKERA EDIT START — secrets by file (see alkera-secrets.ts)
import { alkeraSecret } from "../alkera-secrets"
// == ALKERA EDIT END

function truthy(key: string) {
  const value = process.env[key]?.toLowerCase()
  return value === "true" || value === "1"
}

const OPENCODE_EXPERIMENTAL = truthy("OPENCODE_EXPERIMENTAL")
const copy = process.env["OPENCODE_EXPERIMENTAL_DISABLE_COPY_ON_SELECT"]

function enabledByExperimental(key: string) {
  return process.env[key] === undefined ? OPENCODE_EXPERIMENTAL : truthy(key)
}

export const Flag = {
  OTEL_EXPORTER_OTLP_ENDPOINT: process.env["OTEL_EXPORTER_OTLP_ENDPOINT"],
  OTEL_EXPORTER_OTLP_HEADERS: process.env["OTEL_EXPORTER_OTLP_HEADERS"],

  OPENCODE_AUTO_HEAP_SNAPSHOT: truthy("OPENCODE_AUTO_HEAP_SNAPSHOT"),
  OPENCODE_GIT_BASH_PATH: process.env["OPENCODE_GIT_BASH_PATH"],
  OPENCODE_CONFIG: process.env["OPENCODE_CONFIG"],
  // == ALKERA EDIT START
  // Product settings: the Alkera harness adapter passes every setting under its
  // ALKERA_ prefix, so a value the user's shell exports under opencode's own
  // OPENCODE_* names never collides with one the harness sets (the adapter also
  // scrubs those). JS keys stay OPENCODE_* so upstream code is unchanged. See
  // vendor/opencode/README.alkera.md.
  // Secrets by file: the harness hands the config over in ALKERA_SECRETS_FILE.
  OPENCODE_CONFIG_CONTENT: alkeraSecret("ALKERA_CONFIG_CONTENT"),
  OPENCODE_DISABLE_AUTOUPDATE: truthy("ALKERA_DISABLE_AUTOUPDATE"),
  // == ALKERA EDIT END
  OPENCODE_ALWAYS_NOTIFY_UPDATE: truthy("OPENCODE_ALWAYS_NOTIFY_UPDATE"),
  OPENCODE_DISABLE_PRUNE: truthy("OPENCODE_DISABLE_PRUNE"),
  OPENCODE_DISABLE_TERMINAL_TITLE: truthy("OPENCODE_DISABLE_TERMINAL_TITLE"),
  OPENCODE_SHOW_TTFD: truthy("OPENCODE_SHOW_TTFD"),
  OPENCODE_DISABLE_AUTOCOMPACT: truthy("OPENCODE_DISABLE_AUTOCOMPACT"),
  // == ALKERA EDIT START — product setting: ALKERA_* env name (key unchanged)
  OPENCODE_DISABLE_MODELS_FETCH: truthy("ALKERA_DISABLE_MODELS_FETCH"),
  // == ALKERA EDIT END
  OPENCODE_DISABLE_MOUSE: truthy("OPENCODE_DISABLE_MOUSE"),
  OPENCODE_FAKE_VCS: process.env["OPENCODE_FAKE_VCS"],
  // == ALKERA EDIT START — product setting: ALKERA_* env name (key unchanged); secrets by file
  OPENCODE_SERVER_PASSWORD: alkeraSecret("ALKERA_SERVER_PASSWORD"),
  // == ALKERA EDIT END
  OPENCODE_SERVER_USERNAME: process.env["OPENCODE_SERVER_USERNAME"],

  // Experimental
  OPENCODE_EXPERIMENTAL_FILEWATCHER: Config.boolean("OPENCODE_EXPERIMENTAL_FILEWATCHER").pipe(
    Config.withDefault(false),
  ),
  OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER: Config.boolean("OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER").pipe(
    Config.withDefault(false),
  ),
  OPENCODE_EXPERIMENTAL_DISABLE_COPY_ON_SELECT:
    copy === undefined ? process.platform === "win32" : truthy("OPENCODE_EXPERIMENTAL_DISABLE_COPY_ON_SELECT"),
  OPENCODE_MODELS_URL: process.env["OPENCODE_MODELS_URL"],
  OPENCODE_MODELS_PATH: process.env["OPENCODE_MODELS_PATH"],
  OPENCODE_DB: process.env["OPENCODE_DB"],

  OPENCODE_WORKSPACE_ID: process.env["OPENCODE_WORKSPACE_ID"],
  OPENCODE_EXPERIMENTAL_WORKSPACES: enabledByExperimental("OPENCODE_EXPERIMENTAL_WORKSPACES"),

  // Evaluated at access time (not module load) because tests, the CLI, and
  // external tooling set these env vars at runtime.
  get OPENCODE_DISABLE_PROJECT_CONFIG() {
    // == ALKERA EDIT START — product setting: ALKERA_* env name (key unchanged)
    return truthy("ALKERA_DISABLE_PROJECT_CONFIG")
    // == ALKERA EDIT END
  },
  // == ALKERA EDIT START
  // Decouple instruction-file discovery (AGENTS.md/CLAUDE.md/CONTEXT.md/ALKERA.md)
  // from project *config* (opencode.json) loading. The harness locks down the
  // user's opencode.json via ALKERA_DISABLE_PROJECT_CONFIG but still wants to read
  // repo instruction files, so instruction discovery reads this separate flag
  // (which the adapter does NOT set → discovery stays ON). Evaluated at access
  // time like the config flag above (tests/CLI set env at runtime).
  get OPENCODE_DISABLE_PROJECT_INSTRUCTIONS() {
    return truthy("ALKERA_DISABLE_PROJECT_INSTRUCTIONS")
  },
  // == ALKERA EDIT END
  get OPENCODE_TUI_CONFIG() {
    return process.env["OPENCODE_TUI_CONFIG"]
  },
  get OPENCODE_CONFIG_DIR() {
    return process.env["OPENCODE_CONFIG_DIR"]
  },
  get OPENCODE_PURE() {
    // == ALKERA EDIT START — product setting: ALKERA_* env name (key unchanged)
    return truthy("ALKERA_PURE")
    // == ALKERA EDIT END
  },
  get OPENCODE_PERMISSION() {
    // == ALKERA EDIT START — product setting: ALKERA_* env name (key unchanged)
    return process.env["ALKERA_PERMISSION"]
    // == ALKERA EDIT END
  },
  get OPENCODE_PLUGIN_META_FILE() {
    return process.env["OPENCODE_PLUGIN_META_FILE"]
  },
  get OPENCODE_CLIENT() {
    return process.env["OPENCODE_CLIENT"] ?? "cli"
  },
}
