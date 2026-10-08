// == ALKERA EDIT START
// Product settings: these constants are self-set onto the process env
// (ensureRunID / ensureProcessRole) and inherited by the agent's bash-tool
// subprocesses. They use the harness's ALKERA_ settings prefix, like every
// other setting the adapter passes. The exported identifiers stay the same so
// upstream code is unchanged. log.ts must read the same name. See README.alkera.md.
export const OPENCODE_RUN_ID = "ALKERA_RUN_ID"
export const OPENCODE_PROCESS_ROLE = "ALKERA_PROCESS_ROLE"
// == ALKERA EDIT END

export function ensureRunID() {
  return (process.env[OPENCODE_RUN_ID] ??= crypto.randomUUID())
}

export function ensureProcessRole(fallback: "main" | "worker") {
  return (process.env[OPENCODE_PROCESS_ROLE] ??= fallback)
}

export function ensureProcessMetadata(fallback: "main" | "worker") {
  return {
    runID: ensureRunID(),
    processRole: ensureProcessRole(fallback),
  }
}

export function sanitizedProcessEnv(overrides?: Record<string, string>) {
  const env = Object.fromEntries(
    Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined),
  )
  return overrides ? Object.assign(env, overrides) : env
}
