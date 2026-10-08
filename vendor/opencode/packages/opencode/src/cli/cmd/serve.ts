import { writeFileSync } from "node:fs"
import { Effect } from "effect"
import { Server } from "../../server/server"
import { effectCmd } from "../effect-cmd"
import { withNetworkOptions, resolveNetworkOptions } from "../network"
import { Flag } from "@opencode-ai/core/flag/flag"

export const ServeCommand = effectCmd({
  command: "serve",
  builder: (yargs) => withNetworkOptions(yargs),
  describe: "starts a headless opencode server",
  // Server loads instances per-request via x-opencode-directory header — no
  // need for an ambient project InstanceContext at startup.
  instance: false,
  handler: Effect.fn("Cli.serve")(function* (args) {
    if (!Flag.OPENCODE_SERVER_PASSWORD) {
      console.log("Warning: OPENCODE_SERVER_PASSWORD is not set; server is unsecured.")
    }
    const opts = yield* resolveNetworkOptions(args)
    const server = yield* Effect.promise(() => Server.listen(opts))
    const listenUrl = `http://${server.hostname}:${server.port}`
    // == ALKERA EDIT START
    // The adapter's PRIMARY readiness signal is this file (path from
    // ALKERA_LISTEN_FILE): a synchronous, fully-flushed write that is immune to
    // stdout buffering. A `bun build --compile` standalone block-buffers stdout
    // to a PIPE on Windows, so the banner below can sit unflushed forever and
    // the adapter would time out waiting for a listen URL. The stdout banner is
    // kept, as upstream prints it, as a fallback + human/log signal; the
    // adapter finds "server listening on " anywhere in the line
    // (opencode_http.py:_await_listen_url). See
    // README.alkera.md.
    const listenFile = process.env["ALKERA_LISTEN_FILE"]
    if (listenFile) {
      try {
        writeFileSync(listenFile, listenUrl)
      } catch {
        // best-effort; the stdout banner remains the fallback signal
      }
    }
    console.log(`opencode server listening on ${listenUrl}`)
    // == ALKERA EDIT END

    yield* Effect.never
  }),
})
