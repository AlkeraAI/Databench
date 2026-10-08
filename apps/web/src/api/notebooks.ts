// The notebook routes (`/api/v1/notebooks/{drive_id}/{item_id}/...`).
//
// Every shape is the generated SDK's: the backend answers the engine's own
// models (the view, a kernel's state, a frame's attachment), so a rename on
// the server is a type error here and in the one adapter that turns them into
// the editor's view model (`notebook/notebookWire.ts`).
//
// Runs, kernel actions and installs answer only "accepted": what follows
// (statuses, outputs, the queue) arrives on the notebook channel, so these
// mutations refresh nothing; a kernel action refreshes the view's kernel.

import type { components } from "@alkera/sdk";
import { useMutation, useQuery } from "@tanstack/react-query";

import { ORG_HEADER, activeOrgId } from "./activeOrg";
import { apiBaseUrl } from "./client";
import { ApiError } from "./errors";
import { keys } from "./keys";
import { gatedFetch } from "./readGate";

type Schemas = components["schemas"];

export type NotebookView = Schemas["NotebookView"];
export type StoredNotebook = Schemas["StoredNotebook"];
export type NotebookCellState = Schemas["CellState"];
export type NotebookKernelInfo = Schemas["KernelInfo"];
export type NotebookPresence = Schemas["Presence"];
export type NotebookEnvInfo = Schemas["EnvInfo"];
export type NotebookPackageInfo = Schemas["PackageInfo"];
export type GraphSummary = Schemas["GraphSummary"];
export type GraphCellSummary = Schemas["GraphCellSummary"];
export type GraphError = Schemas["GraphErrorInfo"];
export type RunRequest = Schemas["RunRequest"];
export type RunAccepted = Schemas["RunAccepted"];
export type KernelResult = Schemas["KernelResult"];
export type NotebookKernelAction = Schemas["KernelRequest"]["action"];
export type FrameAttachRequest = Schemas["FrameAttachRequest"];
export type FrameAttached = Schemas["FrameAttached"];
export type CommRequest = Schemas["CommRequest"];
export type RequestAccepted = Schemas["Accepted"];
export type NotebookEnvListing = Schemas["EnvListing"];
export type NotebookEnvPackages = Schemas["EnvPackages"];
export type NotebookTablePage = Schemas["TablePage"];
export type NotebookConnections = Schemas["NotebookConnections"];
export type NotebookConnection = Schemas["NotebookConnection"];
export type NotebookEditor = Schemas["NotebookEditor"];

/** A refused notebook request, with the server's code when it sent one. */
/** A refused notebook request: an `ApiError`, so the sentence it is told
 *  in comes from `refusalSentence` like every other refusal. */
export class NotebookRequestError extends ApiError {
  constructor(status: number, body: unknown, headers?: Headers) {
    super(status, body, "The notebook request failed", headers);
    this.name = "NotebookRequestError";
  }
}

/** The code a notebook request is answered with while the server wakes the
 *  chat holding the notebook's folder: the same request is asked again. */
export const NOTEBOOK_WAKING = "notebook.waking";
/** How long a request waits on a wake before it is given up. */
export const WAKE_PATIENCE_MS = 180_000;
/** The wait between asks when the server names none. */
const WAKE_RETRY_MS = 3_000;

/** Whether `error` is the server saying the notebook's machine is waking. */
export function isWaking(error: unknown): error is ApiError {
  return error instanceof ApiError && error.code === NOTEBOOK_WAKING;
}

export interface WakeWait {
  /** Told `true` once when the request starts waiting on the wake, and
   *  `false` once when it stops (answered, refused or given up). */
  onWaking: (waking: boolean) => void;
  patienceMs?: number;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** Asks `attempt` until the server stops answering that the notebook's
 *  machine is waking, waiting as long as it says between asks. Whatever else
 *  it answers is returned or thrown as it came; a wake that outlasts the
 *  patience throws its last answer. */
export async function untilAwake<T>(attempt: () => Promise<T>, wait: WakeWait): Promise<T> {
  const now = wait.now ?? Date.now;
  const sleep = wait.sleep ?? pause;
  const deadline = now() + (wait.patienceMs ?? WAKE_PATIENCE_MS);
  let waiting = false;
  try {
    for (;;) {
      try {
        return await attempt();
      } catch (error) {
        if (!isWaking(error) || now() >= deadline) throw error;
        if (!waiting) {
          waiting = true;
          wait.onWaking(true);
        }
        const seconds = Number(error.retryAfter);
        await sleep(Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : WAKE_RETRY_MS);
      }
    }
  } finally {
    if (waiting) wait.onWaking(false);
  }
}

function base(driveId: string, itemId: string): string {
  return `${apiBaseUrl}/api/v1/notebooks/${encodeURIComponent(driveId)}/${encodeURIComponent(itemId)}`;
}

/** Where an output stored beside the notebook is read from, by its hash:
 *  the notebook's blob route, which answers with a redirect to the bytes (or,
 *  for an image the notebook carries inline, the bytes themselves). */
export function outputBlobUrl(driveId: string, itemId: string, sha256: string): string {
  return `${base(driveId, itemId)}/blobs/${sha256}`;
}

const record = (v: unknown): Record<string, unknown> | null => (typeof v === "object" && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : null);

async function call<T>(url: string, init: RequestInit = {}, fetchImpl: typeof fetch = fetch): Promise<T> {
  const headers = new Headers(init.headers);
  const org = activeOrgId();
  if (org !== null) headers.set(ORG_HEADER, org);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await fetchImpl(url, { ...init, headers, credentials: "include" });
  if (!response.ok) {
    let body: unknown = null;
    try {
      // `{error: {code, message}}`, FastAPI's `{detail: {...}}`, or flat.
      const parsed = record(await response.json()) ?? {};
      body = record(parsed.detail) ?? parsed;
    } catch {
      // A body that is not JSON carries no code and no sentence.
    }
    throw new NotebookRequestError(response.status, body, response.headers);
  }
  return (await response.json()) as T;
}

/** The notebook as the drive stores it: its cells and the outputs saved
 *  beside it. Read with no live document and no kernel. */
export function getStoredNotebook(driveId: string, itemId: string, fetchImpl?: typeof fetch): Promise<StoredNotebook> {
  return call<StoredNotebook>(`${base(driveId, itemId)}/stored`, {}, fetchImpl);
}

/** Where the notebook editor opens the notebook `itemId`, or a new notebook
 *  in the folder `itemId`: the chat whose workspace pane runs it, or `null`
 *  where no kernel can run or this reader may not send in that chat. */
export function getNotebookEditor(driveId: string, itemId: string, fetchImpl?: typeof fetch): Promise<NotebookEditor> {
  return call<NotebookEditor>(`${base(driveId, itemId)}/editor`, {}, fetchImpl);
}

/** The query for {@link getNotebookEditor}, for a hook and a one-off fetch. */
export function notebookEditorQuery(driveId: string, itemId: string) {
  return {
    queryKey: keys.notebooks.editor(driveId, itemId),
    queryFn: () => getNotebookEditor(driveId, itemId),
    staleTime: 30_000,
    retry: false,
  } as const;
}

/** {@link getNotebookEditor} for a folder or a notebook on screen. */
export function useNotebookEditor(driveId: string | undefined, itemId: string | undefined) {
  return useQuery({
    ...notebookEditorQuery(driveId ?? "", itemId ?? ""),
    enabled: driveId !== undefined && driveId !== "" && itemId !== undefined && itemId !== "",
  });
}

/** An output stored beside the notebook, by its hash, as its bytes; `null`
 *  when the notebook no longer holds it (no cell shows it: the cell ran
 *  again, or was deleted). Any other refusal throws, with its status. The
 *  route decides the reader's access on every read, as the notebook's own
 *  view does. */
export async function getOutputBlob(driveId: string, itemId: string, sha256: string, fetchImpl: typeof fetch = gatedFetch): Promise<Blob | null> {
  // The route redirects a stored file to the content origin. The browser
  // follows with no cookie and no header of this page's, as every content
  // read does, so the request carries neither.
  const response = await fetchImpl(outputBlobUrl(driveId, itemId, sha256), { credentials: "same-origin" });
  if (response.status === 404) return null;
  if (!response.ok) throw new NotebookRequestError(response.status, null, response.headers);
  return response.blob();
}

export function getNotebookView(driveId: string, itemId: string, fetchImpl?: typeof fetch): Promise<NotebookView> {
  return call<NotebookView>(base(driveId, itemId), {}, fetchImpl);
}

export function postRun(driveId: string, itemId: string, body: RunRequest, fetchImpl?: typeof fetch): Promise<RunAccepted> {
  return call<RunAccepted>(`${base(driveId, itemId)}/runs`, { method: "POST", body: JSON.stringify(body) }, fetchImpl);
}

export function postKernel(driveId: string, itemId: string, action: NotebookKernelAction, fetchImpl?: typeof fetch): Promise<KernelResult> {
  return call<KernelResult>(`${base(driveId, itemId)}/kernel`, { method: "POST", body: JSON.stringify({ action }) }, fetchImpl);
}

/** Clears cells' outputs for everyone (every cell's with no `cellIds`). */
export function postClearOutputs(driveId: string, itemId: string, cellIds: string[] | null, fetchImpl?: typeof fetch): Promise<RequestAccepted> {
  return call<RequestAccepted>(`${base(driveId, itemId)}/outputs/clear`, { method: "POST", body: JSON.stringify({ cell_ids: cellIds }) }, fetchImpl);
}

export function postEnvInstall(driveId: string, itemId: string, packages: string[], fetchImpl?: typeof fetch): Promise<RequestAccepted> {
  return call<RequestAccepted>(`${base(driveId, itemId)}/env/install`, { method: "POST", body: JSON.stringify({ packages }) }, fetchImpl);
}

/** Build the notebook's environment from its spec now, remove packages
 *  from it, or cancel its build under way. The outcome arrives on the
 *  notebook channel as `env.install` naming the action. */
export function postEnvChange(
  driveId: string,
  itemId: string,
  action: "build" | "remove" | "cancel",
  packages: string[],
  fetchImpl?: typeof fetch,
): Promise<RequestAccepted> {
  return call<RequestAccepted>(`${base(driveId, itemId)}/env/${action}`, { method: "POST", body: JSON.stringify({ packages }) }, fetchImpl);
}

/** Attach an output frame at the engine's widget hub. The server names the
 *  frame: its comm messages and its `frame.message` events carry that id.
 *  Attaching needs run rights; a reader is refused with a 403. */
export function postFrame(driveId: string, itemId: string, body: FrameAttachRequest, fetchImpl?: typeof fetch): Promise<FrameAttached> {
  return call<FrameAttached>(`${base(driveId, itemId)}/frames`, { method: "POST", body: JSON.stringify(body) }, fetchImpl);
}

export function deleteFrame(driveId: string, itemId: string, frameId: string, fetchImpl?: typeof fetch): Promise<unknown> {
  return call(`${base(driveId, itemId)}/frames/${encodeURIComponent(frameId)}`, { method: "DELETE" }, fetchImpl);
}

export function postComm(driveId: string, itemId: string, body: CommRequest, fetchImpl?: typeof fetch): Promise<unknown> {
  return call(`${base(driveId, itemId)}/comm`, { method: "POST", body: JSON.stringify(body) }, fetchImpl);
}

/** The notebook's environment and every one found for it. */
export function getNotebookEnvs(driveId: string, itemId: string, fetchImpl?: typeof fetch): Promise<NotebookEnvListing> {
  return call<NotebookEnvListing>(`${base(driveId, itemId)}/envs`, {}, fetchImpl);
}

export function getEnvPackages(driveId: string, itemId: string, envId: string, fetchImpl?: typeof fetch): Promise<NotebookEnvPackages> {
  return call<NotebookEnvPackages>(`${base(driveId, itemId)}/envs/${encodeURIComponent(envId)}/packages`, {}, fetchImpl);
}

/** The connections the notebook's SQL cells may name, each with whether
 *  this reader can use it. */
export function getNotebookConnections(driveId: string, itemId: string, fetchImpl?: typeof fetch): Promise<NotebookConnections> {
  return call<NotebookConnections>(`${base(driveId, itemId)}/connections`, {}, fetchImpl);
}

export function useNotebookConnections(driveId: string, itemId: string, enabled = true, fetchImpl?: typeof fetch) {
  return useQuery({
    queryKey: keys.notebooks.connections(driveId, itemId),
    queryFn: () => getNotebookConnections(driveId, itemId, fetchImpl),
    enabled,
    staleTime: 60_000,
  });
}

/** A page of a cell's table output. `sort` is `col:asc,col2:desc`. */
export interface TablePageQuery {
  offset: number;
  limit: number;
  sort?: string | null;
  filter_sql?: string | null;
}

export function getTablePage(
  driveId: string,
  itemId: string,
  cellId: string,
  query: TablePageQuery,
  fetchImpl?: typeof fetch,
): Promise<NotebookTablePage> {
  const params = new URLSearchParams({ offset: String(query.offset), limit: String(query.limit) });
  if (query.sort) params.set("sort", query.sort);
  if (query.filter_sql) params.set("filter_sql", query.filter_sql);
  return call<NotebookTablePage>(`${base(driveId, itemId)}/cells/${encodeURIComponent(cellId)}/table?${params}`, {}, fetchImpl);
}

/** The notebook's view: its kernel's state and who is where. The cells are
 *  read from the live document, not from here. */
export function useNotebookView(driveId: string, itemId: string, enabled = true) {
  return useQuery({
    queryKey: keys.notebooks.view(driveId, itemId),
    queryFn: () => getNotebookView(driveId, itemId),
    enabled,
    staleTime: 30_000,
  });
}

/** The notebook as stored, for a preview. Keyed on the file's version, so a
 *  new version is read again and an unchanged one is not. */
export function useStoredNotebook(driveId: string, itemId: string, version: string) {
  return useQuery({
    queryKey: keys.notebooks.stored(driveId, itemId, version),
    queryFn: () => getStoredNotebook(driveId, itemId),
    staleTime: Infinity,
    retry: false,
  });
}

/** The notebook's environments, asked of the box serving it. Asked again
 *  while the server wakes the box (`wake` hears the wait). */
export function useNotebookEnvs(driveId: string, itemId: string, enabled = true, fetchImpl?: typeof fetch, wake?: WakeWait) {
  return useQuery({
    queryKey: keys.notebooks.envs(driveId, itemId),
    queryFn: () => {
      const ask = () => getNotebookEnvs(driveId, itemId, fetchImpl);
      return wake ? untilAwake(ask, wake) : ask();
    },
    enabled,
    staleTime: 60_000,
    retry: false,
  });
}

export function useEnvPackages(driveId: string, itemId: string, envId: string | null, fetchImpl?: typeof fetch, wake?: WakeWait) {
  return useQuery({
    queryKey: keys.notebooks.packages(driveId, itemId, envId ?? ""),
    queryFn: () => {
      const ask = () => getEnvPackages(driveId, itemId, envId ?? "", fetchImpl);
      return wake ? untilAwake(ask, wake) : ask();
    },
    enabled: envId !== null,
    staleTime: 60_000,
    retry: false,
  });
}

export function useRunNotebook(driveId: string, itemId: string) {
  return useMutation({
    mutationFn: (body: RunRequest) => postRun(driveId, itemId, body),
    meta: { invalidates: "none" },
  });
}

export function useNotebookKernel(driveId: string, itemId: string) {
  return useMutation({
    mutationFn: (action: NotebookKernelAction) => postKernel(driveId, itemId, action),
    meta: { invalidates: [keys.notebooks.view(driveId, itemId)] },
  });
}

export function useNotebookInstall(driveId: string, itemId: string) {
  return useMutation({
    mutationFn: (packages: string[]) => postEnvInstall(driveId, itemId, packages),
    meta: { invalidates: "none" },
  });
}
