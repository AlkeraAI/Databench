// Chat templates: the portal's door to the routes behind "save this chat so the
// next one starts here".
//
// A template is two things at once, and the hooks below are why no surface has
// to know it: the ROW carries the title, the brief and the pin a new chat
// starts from, and the FOLDER beside it carries the files that chat opens with.
// Every write therefore refreshes both families — the templates a page lists
// AND the drive, whose folder was created, renamed or taken away by the same
// request.
//
// Two wire rules shape the writes:
//
//  * A save COPIES a chat's files. The attempt carries an `Idempotency-Key`
//    minted per attempt (`withIdempotency`), so react-query re-running the
//    request replays the server's stored answer instead of copying the files
//    again — the one failure mode a person would not notice until their drive
//    was full of templates.
//  * An edit names the version it read (`expected_version`). Two people editing
//    one brief is a 409 the caller can re-read from, never a silent overwrite.
//
// The answer shapes are the generated ones, so a route or schema change lands
// here as a type error rather than as a wrong screen. The requests themselves
// go out as plain `fetch` calls — the shape the chat's own attachment and
// workspace routes use for the same kind of id-addressed resource.

import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { apiBaseUrl, apiFetch, failedResponse } from "./client";
import { ApiError } from "./errors";
import { withIdempotency, type Attempt } from "./files";
import { keys } from "./keys";

export type ChatTemplateRead = components["schemas"]["ChatTemplateRead"];
export type ChatTemplateList = components["schemas"]["ChatTemplateList"];
type SaveAsTemplateBody = components["schemas"]["SaveAsTemplate"];
type ChatTemplateUpdateBody = components["schemas"]["ChatTemplateUpdate"];

/** One listing page. The server's own default; the browser asks for it
 *  explicitly so the page size is the client's decision rather than a default
 *  that could move under it. */
export const TEMPLATES_PAGE = 50;

const TEMPLATES_PATH = "/api/v1/chat-templates";

/** A template write changes the row AND the folder it is filed in, so both
 *  families refresh: the templates a page is listing, and every Files read that
 *  could be showing the folder. */
const TEMPLATE_INVALIDATES = [keys.chatTemplates.all, keys.files.all] as const;

function templatePath(templateId: string): string {
  return `${TEMPLATES_PATH}/${encodeURIComponent(templateId)}`;
}

/**
 * One request, its refusal raised as an {@link ApiError}.
 *
 * A 204 is returned without a parse: the delete answers no body at all, and a
 * `.json()` on that is a parse error dressed up as a failed delete.
 */
async function send<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(new URL(path, apiBaseUrl).toString(), {
    credentials: "include",
    ...init,
    headers: { "content-type": "application/json", ...(init.headers ?? {}) },
  });
  if (!response.ok) throw await failedResponse(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** The read's retry rule: a template that is gone, or that this reader may not
 *  have, is a DECISION the server has made and does not become true again by
 *  asking twice more — a page can say so at once instead of after three
 *  requests and two backoff waits. A 5xx is still a flake and still retries. */
function retryUnlessAnswered(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && [401, 403, 404].includes(error.status)) return false;
  return failureCount < 3;
}

export interface ChatTemplatesOptions {
  /** Whether the read runs at all. Default true. */
  enabled?: boolean;
}

/**
 * The templates this caller may read, newest first.
 *
 * Paged by the server's opaque cursor: a page that comes back without one is
 * the last, so `hasNextPage` goes false rather than the caller re-reading the
 * final page for ever. The cursor rides the page param and NOT the key, so one
 * cache entry holds the whole listing and a save refreshes all of it.
 */
export function useChatTemplates(options: ChatTemplatesOptions = {}) {
  return useInfiniteQuery<
    ChatTemplateList,
    ApiError,
    ChatTemplateRead[],
    readonly unknown[],
    string | undefined
  >({
    queryKey: keys.chatTemplates.all,
    enabled: options.enabled ?? true,
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    select: (data) => data.pages.flatMap((page) => page.items),
    queryFn: ({ pageParam }) => {
      const query = new URLSearchParams({ limit: String(TEMPLATES_PAGE) });
      if (pageParam) query.set("cursor", pageParam);
      return send<ChatTemplateList>(`${TEMPLATES_PATH}?${query.toString()}`);
    },
  });
}

/** One template, by id. Disabled until the id is known, so a page that has not
 *  routed yet holds its own cache entry instead of colliding with a real one. */
export function useChatTemplate(
  templateId: string | undefined,
  options: ChatTemplatesOptions = {},
) {
  return useQuery<ChatTemplateRead, ApiError>({
    queryKey: keys.chatTemplates.one(templateId),
    enabled: (options.enabled ?? true) && Boolean(templateId),
    retry: retryUnlessAnswered,
    queryFn: () => send<ChatTemplateRead>(templatePath(templateId as string)),
  });
}

/**
 * What a save sends. Everything but the chat is optional, and an omitted field
 * is not an empty one: no title means "use the chat's own", no brief means "the
 * server writes it from the transcript", no destination means the caller's own
 * templates folder, made on demand.
 *
 * It extends the attempt shape because the variables object's own identity is
 * what the idempotency key is remembered against — one attempt, one key.
 */
export interface SaveAsTemplateInput extends Attempt, Omit<SaveAsTemplateBody, "source_chat_id"> {
  source_chat_id: string;
}

function saveBody(input: SaveAsTemplateInput): SaveAsTemplateBody {
  return {
    source_chat_id: input.source_chat_id,
    ...(input.title === undefined ? {} : { title: input.title }),
    ...(input.brief === undefined ? {} : { brief: input.brief }),
    ...(input.destination_id === undefined ? {} : { destination_id: input.destination_id }),
    ...(input.client_id === undefined ? {} : { client_id: input.client_id }),
  };
}

/** Save a chat as a template: its brief, and a copy of the files it worked in. */
export function useSaveAsTemplate() {
  return useMutation<ChatTemplateRead, ApiError, SaveAsTemplateInput>({
    meta: { invalidates: TEMPLATE_INVALIDATES },
    mutationFn: (input) =>
      send<ChatTemplateRead>(TEMPLATES_PATH, {
        method: "POST",
        headers: withIdempotency(input),
        body: JSON.stringify(saveBody(input)),
      }),
  });
}

/** What an edit sends: the fields the person changed, fenced on the version the
 *  row was read at. A field left out is a field the server keeps. */
export interface UpdateChatTemplateInput extends Attempt {
  templateId: string;
  title?: string;
  brief?: string;
  expectedVersion: number;
}

function updateBody(input: UpdateChatTemplateInput): ChatTemplateUpdateBody {
  return {
    ...(input.title === undefined ? {} : { title: input.title }),
    ...(input.brief === undefined ? {} : { brief: input.brief }),
    expected_version: input.expectedVersion,
  };
}

function putTemplate(input: UpdateChatTemplateInput): Promise<ChatTemplateRead> {
  return send<ChatTemplateRead>(templatePath(input.templateId), {
    method: "PUT",
    headers: withIdempotency(input),
    body: JSON.stringify(updateBody(input)),
  });
}

/** Edit a template's title or its brief. */
export function useUpdateChatTemplate() {
  return useMutation<ChatTemplateRead, ApiError, UpdateChatTemplateInput>({
    meta: { invalidates: TEMPLATE_INVALIDATES },
    mutationFn: putTemplate,
  });
}

/** What a rename sends. The version is optional: a surface that READ the row
 *  fences on what it read, and one that only knows the title — a Files row,
 *  which carries the template's title on its facet and nothing else — leaves it
 *  out and lets the hook read it. */
export interface RenameChatTemplateInput extends Attempt {
  templateId: string;
  title: string;
  expectedVersion?: number;
}

/**
 * Rename a chat template.
 *
 * A template's title is not editable through the generic object route — it is
 * the template's own, and the folder's README is re-rendered from it — so the
 * rename goes here rather than to `PUT /objects/{id}`.
 *
 * The write is fenced on a version, and a caller that does not hold one has the
 * row read for it here rather than guessing. A guess is wrong from the second
 * rename onwards — the first accepted write moves the template past it — and the
 * 409 it earns cannot be cleared by re-reading anything the caller can see.
 */
export function useRenameChatTemplate() {
  return useMutation<ChatTemplateRead, ApiError, RenameChatTemplateInput>({
    meta: { invalidates: TEMPLATE_INVALIDATES },
    mutationFn: async (input) => {
      const expectedVersion =
        input.expectedVersion ??
        (await send<ChatTemplateRead>(templatePath(input.templateId))).version;
      return putTemplate({ ...input, expectedVersion });
    },
  });
}

export interface DeleteChatTemplateInput extends Attempt {
  templateId: string;
}

/** Take the template away from everyone it was shared with. The server answers
 *  nothing, so the caller learns only that it is gone — which is the whole
 *  fact. A refusal rejects rather than resolving, so the surface that asked can
 *  keep the row on screen and say why. */
export function useDeleteChatTemplate() {
  return useMutation<void, ApiError, DeleteChatTemplateInput>({
    meta: { invalidates: TEMPLATE_INVALIDATES },
    mutationFn: (input) =>
      send<void>(templatePath(input.templateId), {
        method: "DELETE",
        headers: withIdempotency(input),
      }),
  });
}
