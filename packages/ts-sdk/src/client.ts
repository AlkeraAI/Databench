// Typed client factory for the Alkera API.
//
// `paths` is generated from packages/shared-openapi/openapi.json.
// The wrapping client (`openapi-fetch`) gives us full request + response
// type checking against that schema with zero runtime cost beyond fetch.

import createOpenApiClient from "openapi-fetch";
import type { Client, ClientOptions } from "openapi-fetch";
import type { paths } from "./schema";

export type AlkeraClient = Client<paths>;

export interface CreateClientOptions {
  baseUrl?: string;
  fetch?: typeof fetch;
  headers?: Record<string, string>;
  /**
   * Whether the underlying fetch sends/receives the session cookie.
   * Defaults to `"include"` so cross-origin requests (e.g. SPA on :5173 →
   * API on :8000 in dev when not using the Vite proxy) carry the cookie.
   * Override only if you specifically want to bypass auth.
   */
  credentials?: RequestCredentials;
}

/**
 * Build a typed Alkera API client.
 *
 * Pass `baseUrl` to point at a non-default API; defaults to same-origin so
 * Vite's dev proxy can forward /api and /health to the backend.
 *
 * The default `fetch` is looked up per-call from `globalThis.fetch` (rather
 * than captured once at construction), so test runners that swap the global
 * `fetch` via `vi.stubGlobal` after the client is created work transparently.
 */
export function createClient(options: CreateClientOptions = {}): AlkeraClient {
  const credentials: RequestCredentials = options.credentials ?? "include";
  const userFetch = options.fetch;
  const fetchImpl: typeof fetch = (input, init) => {
    const merged: RequestInit = { credentials, ...(init ?? {}) };
    return userFetch
      ? userFetch(input, merged)
      : globalThis.fetch(input, merged);
  };
  const opts: ClientOptions = {
    baseUrl: options.baseUrl ?? "",
    fetch: fetchImpl,
    headers: options.headers,
  };
  return createOpenApiClient<paths>(opts);
}
