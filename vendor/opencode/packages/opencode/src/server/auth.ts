export * as ServerAuth from "./auth"

import { ConfigService } from "@/effect/config-service"
import { Flag } from "@opencode-ai/core/flag/flag"
// == ALKERA EDIT START — secrets by file
import { alkeraSecret } from "@opencode-ai/core/alkera-secrets"
// == ALKERA EDIT END
import { Config as EffectConfig, Context, Option, Redacted } from "effect"

export type Credentials = {
  password?: string
  username?: string
}

export type DecodedCredentials = {
  readonly username: string
  readonly password: Redacted.Redacted
}

export class Config extends ConfigService.Service<Config>()("@opencode/ServerAuthConfig", {
  // == ALKERA EDIT START — product setting: server-side PASSWORD read under the ALKERA_ name (the adapter's
  // _build_env sets ALKERA_SERVER_PASSWORD; the Flag-based client read in header() is
  // covered by flag.ts pointing the value at the renamed name). See README.alkera.md.
  // Secrets by file: the password the harness handed over in ALKERA_SECRETS_FILE
  // (packages/core/src/alkera-secrets.ts) wins over any the config source holds.
  password: EffectConfig.string("ALKERA_SERVER_PASSWORD").pipe(
    EffectConfig.option,
    EffectConfig.map((value) => Option.orElse(Option.fromNullishOr(alkeraSecret("ALKERA_SERVER_PASSWORD")), () => value)),
  ),
  // == ALKERA EDIT END
  username: EffectConfig.string("OPENCODE_SERVER_USERNAME").pipe(EffectConfig.withDefault("opencode")),
}) {}

export type Info = Context.Service.Shape<typeof Config>

export function required(config: Info) {
  return Option.isSome(config.password) && config.password.value !== ""
}

export function authorized(credentials: DecodedCredentials, config: Info) {
  return (
    Option.isSome(config.password) &&
    credentials.username === config.username &&
    Redacted.value(credentials.password) === config.password.value
  )
}

export function header(credentials?: Credentials) {
  const password = credentials?.password ?? Flag.OPENCODE_SERVER_PASSWORD
  if (!password) return undefined

  const username = credentials?.username ?? Flag.OPENCODE_SERVER_USERNAME ?? "opencode"
  return `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}`
}

export function headers(credentials?: Credentials) {
  const authorization = header(credentials)
  if (!authorization) return undefined
  return { Authorization: authorization }
}
