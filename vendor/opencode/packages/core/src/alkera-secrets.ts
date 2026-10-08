// == ALKERA EDIT START — secrets by file, not by environment (whole file is alkera-local).
//
// The Alkera harness hands this process its loopback password and its injected
// config (which carries the chat's gateway token and tool-server bearer) in a
// file named by ALKERA_SECRETS_FILE, never in its environment: an environment
// stays readable in /proc/<pid>/environ for the life of the process, by
// anything running as the same uid, and is inherited by every child. The file
// is read once, on first use, and removed; ALKERA_SECRETS_FILE is taken out of
// this process's environment so no child is told where it was. Only the names
// below are taken from it. A file that is named but cannot be read stops the
// process: an agent server without its password would serve unauthenticated.
//
// With no ALKERA_SECRETS_FILE the names are read from the environment as
// upstream does (a local checkout, upstream's own tests).
import { readFileSync, unlinkSync } from "node:fs"

const NAMES = new Set(["ALKERA_CONFIG_CONTENT", "ALKERA_SERVER_PASSWORD"])
let held: Map<string, string> | undefined
let failure: unknown

function load(): Map<string, string> {
  if (failure !== undefined) throw failure
  if (held) return held
  const found = new Map<string, string>()
  const file = process.env["ALKERA_SECRETS_FILE"]
  if (file) {
    delete process.env["ALKERA_SECRETS_FILE"]
    let parsed: unknown
    try {
      parsed = JSON.parse(readFileSync(file, "utf8"))
    } catch (error) {
      // Every later read fails too, so no caller falls back to the environment.
      failure = error
      throw error
    }
    try {
      unlinkSync(file)
    } catch {
      // The values are already held; a file the directory will not let this
      // process remove is left to the harness, which removes it at stop.
    }
    if (typeof parsed === "object" && parsed !== null) {
      for (const [name, value] of Object.entries(parsed)) {
        if (NAMES.has(name) && typeof value === "string") found.set(name, value)
      }
    }
  }
  held = found
  return found
}

/** A secret the harness handed this process: from its secrets file when it
 * has one, else from the environment. */
export function alkeraSecret(name: string): string | undefined {
  return load().get(name) ?? process.env[name]
}

/** Forget what was read, for tests. */
export function resetAlkeraSecretsForTests(): void {
  held = undefined
  failure = undefined
}
// == ALKERA EDIT END
