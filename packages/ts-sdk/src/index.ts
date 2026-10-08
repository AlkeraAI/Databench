// @alkera/sdk — typed TypeScript client for the Alkera API.
//
// Regenerate types after backend route/schema changes:
//   make gen-sdk        (from repo root)
//   - or -
//   pnpm --filter @alkera/sdk gen

export { createClient } from "./client";
export type { AlkeraClient, CreateClientOptions } from "./client";
export type { paths, components, operations } from "./schema";
// The server's own field limits, read out of the same document the types come
// from — so a surface that has to show a ceiling never spells the number itself.
export * from "./schemaLimits";

export const version = "0.0.0";
