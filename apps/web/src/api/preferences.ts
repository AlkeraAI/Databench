// The reader's own preferences, in the browser.
//
// One document, read whole and written by MERGE — the server applies only the
// fields a save names, so two tabs (or a newer client and an older one) editing
// different settings cannot clobber each other's.
//
// The mutation declares the chat surfaces' OWN key alongside the preferences
// key on purpose: the new-chat seed (default model, default stance) is read by
// every open composer under `chatKeys.chatDefaults()`, a family of its own that
// no `keys.chats.*` prefix reaches. A save has to re-seed it immediately rather
// than on the next mount — that is what "used throughout all chats & updated
// immediately" means in practice.

import { useMutation, useQuery, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import type { components } from "@alkera/sdk";

import { chatKeys } from "@/pages/workspace/chat/chatKeys";

import { apiFetch } from "./client";
import { apiUrl } from "./cloudChat/transport";
import { ApiError } from "./errors";
import { keys } from "./keys";

type Schemas = components["schemas"];
export type PreferencesRead = Schemas["PreferencesRead"];

/** A preferences document as the server stores it: the typed fields the product
 *  knows plus whatever a newer client wrote, which rides through untouched. */
export type PreferenceValues = Record<string, unknown>;

const JSON_HEADERS = { "content-type": "application/json" } as const;

async function request<T>(method: string, body?: unknown): Promise<T> {
  const response = await apiFetch(apiUrl("/api/v1/me/preferences"), {
    credentials: "include",
    method,
    ...(body === undefined ? {} : { headers: JSON_HEADERS, body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    let detail: unknown = null;
    try {
      detail = await response.json();
    } catch {
      detail = null;
    }
    throw new ApiError(response.status, detail, "the preferences request failed", response.headers);
  }
  return (await response.json()) as T;
}

/** The document an editor holds. The server answers the Default Chat Model
 *  resolved — a pick that no longer resolves, or none, reads as the platform
 *  default — and flags when it did. An editor shows that as "no preference":
 *  holding the answered model would save it back as a pick on the next save,
 *  and pin somebody who never chose to today's default. */
function editableValues(read: PreferencesRead): PreferenceValues {
  const values = (read.preferences ?? {}) as PreferenceValues;
  if (!read.chat_model_defaulted) return values;
  return { ...values, default_chat_model: null, default_chat_effort: null };
}

export function useMyPreferences(): UseQueryResult<PreferenceValues> {
  return useQuery({
    queryKey: keys.me.preferences,
    queryFn: async () => editableValues(await request<PreferencesRead>("GET")),
  });
}

/** Save some preference fields. The argument is the PATCH — only what changed. */
export function useSetMyPreferences(): UseMutationResult<
  PreferenceValues,
  unknown,
  PreferenceValues
> {
  return useMutation({
    // A saved default model or stance is what the NEXT new chat opens on, and
    // every mounted composer is holding the old answer — so the resolved seed
    // the composers read is refreshed with the preferences themselves. It is
    // spelled by the chat surfaces (`["ide", "chatDefaults"]`), NOT under the
    // `["chats"]` list family: react-query matches by prefix, so naming the list
    // alone would leave the composer on the old model until it remounted.
    // Declared here rather than hand-wired in `onSuccess`: the query client's
    // policy owns invalidation.
    meta: { invalidates: [keys.me.preferences, keys.chats.all, chatKeys.chatDefaults()] },
    mutationFn: async (patch: PreferenceValues) => {
      return editableValues(await request<PreferencesRead>("PATCH", { preferences: patch }));
    },
  });
}
