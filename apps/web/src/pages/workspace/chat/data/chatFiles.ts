// The chat's files, as the portal reaches them: the port every shell fills
// so the composer can put a pasted image on the message and the transcript
// can show it back, and the browser's implementation over Files.
//
// A message names a file inside the chat by a path relative to the chat's
// effective root — the agent's working directory, which is the folder a file
// dropped or uploaded onto the chat lands in. The browser knows the CHAT
// folder's node (`files_node_id` on the chat row) but is deliberately not
// told the working folder's name: the server decides where an upload aimed at
// a chat lands (`drop_target_for`), and this file finds the file back by name
// — directly under the chat node, or one folder down — so a change of that
// layout is a server change and nothing here.
//
// Once found, an image's BYTES are bought here rather than pointed at: a
// single-use grant is minted for the node and spent anonymously against the
// content host, and what the transcript renders is the object URL the bytes
// landed in. A transcript is read by whoever the chat is shared with and is
// copied out of, so a URL in it that carried authorization of its own would be
// an authorization anyone could forward. The version the bytes were bought at
// is remembered, so re-rendering costs nothing and the agent rewriting the file
// replaces what is on screen.

import type { components } from "@alkera/sdk";
import { ChatFileNotReady } from "@alkera/ui";

import { apiFetch, failedResponse } from "../../../../api/client";
import type { ContentGrant, MintContentGrant, MintGrantVars } from "../../../../api/files";
import { UploadClient, type UploadClientOptions } from "../../../../api/filesUpload";
import { apiUrl, getChat } from "../../../../api/cloudChat/transport";
import { isLivePending } from "../../files/preview/promoteWire";
import { viewerUrl } from "../../files/viewer";
import { chatFilesRoot } from "../../workspaces/chatWorkspace";

type ItemRow = Pick<components["schemas"]["Item"], "id" | "kind" | "name" | "driveId" | "etag">;
/** One page of a folder, as the server sends it: the rows ride under `value`. */
type ChildrenPage = components["schemas"]["ChildrenPage"];

/** A node inside the chat's folder, as the port found it back from a path a
 *  message named: the node, the folder it sits in, and the path asked for.
 *  That is what a caller needs to open it beside the chat AND to show the
 *  reader where it lives. `parentId` is null for a shell that can answer the
 *  node but not its place. */
export interface ChatFileLocation {
  nodeId: string;
  driveId: string;
  parentId: string | null;
  name: string;
  path: string;
  kind: ItemRow["kind"];
}

export interface ChatFilesPort {
  /** Put `file` in the chat's root under a name of this port's choosing and
   *  answer the path the message should carry (relative to that root). Rejects
   *  with the reason when the bytes did not land; the caller retries with the
   *  same `File`. */
  upload(
    chatId: string,
    file: File,
    hint: { kind: "image" | "file"; n: number; onProgress?: (percent: number) => void },
  ): Promise<{ path: string }>;
  /** A URL an `<img>` can load for a chat-relative path, or null. The browser's
   *  implementation answers a `blob:` URL: it buys the bytes itself so nothing
   *  another reader could spend ever reaches the page. Rejects with
   *  `ChatFileNotReady` when the file is there but its bytes have not landed. */
  resolveUrl(chatId: string, path: string): Promise<string | null>;
  /** The node a chat-relative path names, or null when nothing is there. Only
   *  a shell that can look a path up offers it; one that can only hand the
   *  path back to its host (the VS Code webview) leaves it off, and a caller
   *  that needs a node falls back to `openPath`. */
  locate?(chatId: string, path: string): Promise<ChatFileLocation | null>;
  /** Open the file the path names, where the shell can. */
  openPath?(chatId: string, path: string): void;
  /** The largest file THIS port's transport can carry, when it has a bound of
   *  its own. The browser's implementation leaves it off: it uploads through
   *  the Files session API, whose ceiling the server publishes and refuses
   *  against before a byte moves — and states in words the composer shows the
   *  reader verbatim — so a number here would be a second ceiling that drifts
   *  from it. The VS Code webview declares one, because staging a file there
   *  is a single base64 message. */
  maxBytes?: number;
}

/** The folder, under the chat's effective root, every upload is put in: what a
 *  person hands the chat stays apart from what the agent writes, and the agent
 *  is told to look there. */
export const UPLOADS_FOLDER = "uploads";

/** How many names an upload draws before it gives up. A name is only ever
 *  taken, never written over: the commit is create-only and a taken name is
 *  drawn again — the box's rule (`write_staged_file`) too. Each draw after the
 *  first sends the bytes again, so the bound is small; four hex digits make a
 *  second collision in a row vanishingly rare. */
export const STAGE_NAME_ATTEMPTS = 8;

/** The random part of a staged name: two bytes as four hex digits, the width
 *  the box draws (`secrets.token_hex(2)` in `staged_file_name`), so a name from
 *  either shell has the one shape. Short on purpose: it is read in a message.
 *  Uniqueness is the commit's to guarantee, not the draw's. */
function stagedSuffix(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(2));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** The path an upload is stored at, relative to the chat's effective root:
 *  deterministic in shape — `uploads/paste-<n>-<id>.<ext>` for an image,
 *  `uploads/file-<n>-<id>.<ext>` for anything else. Two draws can answer the
 *  same name; the upload is what makes sure an earlier file keeps its own. */
export function stagedFileName(kind: "image" | "file", n: number, original: string): string {
  const dot = original.lastIndexOf(".");
  const ext = dot > 0 ? original.slice(dot + 1).toLowerCase().replace(/[^a-z0-9]/g, "") : "";
  const stem = kind === "image" ? "paste" : "file";
  const suffix = stagedSuffix();
  return `${UPLOADS_FOLDER}/${ext ? `${stem}-${n}-${suffix}.${ext}` : `${stem}-${n}-${suffix}`}`;
}

async function readJson<T>(path: string, query?: Record<string, string | number>): Promise<T> {
  const response = await apiFetch(apiUrl(path, query), { credentials: "include" });
  if (!response.ok) throw await failedResponse(response);
  return (await response.json()) as T;
}

interface CloudChatFilesOptions {
  /** How long a folder listing is trusted before a lookup re-reads it. */
  listingTtlMs?: number;
  /** How the just-uploaded file is waited for: attempts and the pause between. */
  settle?: { attempts: number; delayMs: number };
  /** The client one upload attempt sends through, built from the options the
   *  port chose: the drive (so the queued commit is followed to its verdict)
   *  and the hooks it answers with. */
  uploads?: (options: UploadClientOptions) => UploadClient;
  now?: () => number;
  openUrl?: (url: string) => void;
  /** Where a transcript image's bytes are bought. Defaults to the grant route. */
  mint?: MintContentGrant;
}

/** One node the walk found, with the version it was found at: the etag is what
 *  says whether bytes already bought are still this file's bytes. It stays off
 *  `ChatFileLocation`, which is a place, not a version. */
interface FoundNode {
  at: ChatFileLocation;
  etag: string;
}

/** Ask the server for a single-use URL for one node's bytes: what a transcript
 *  image is bought with when the port is given no other `mint`.
 *
 *  Spelled here rather than through the generated client because a grant is not
 *  a read of any row: it answers a fresh URL every call and caches nothing. */
export async function mintContentGrant(vars: MintGrantVars): Promise<ContentGrant> {
  const path = `/api/v1/files/drives/${encodeURIComponent(vars.driveId)}/items/${encodeURIComponent(vars.itemId)}/content-grants`;
  const response = await apiFetch(apiUrl(path), {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind: vars.kind, disposition: vars.disposition ?? "inline" }),
  });
  if (!response.ok) throw await failedResponse(response);
  return (await response.json()) as ContentGrant;
}

/** The browser's port, over the Files REST surface. */
export function cloudChatFiles(options: CloudChatFilesOptions = {}): ChatFilesPort {
  const ttl = options.listingTtlMs ?? 15_000;
  const settle = options.settle ?? { attempts: 20, delayMs: 250 };
  const now = options.now ?? (() => Date.now());
  const openUrl = options.openUrl ?? ((url: string) => void window.open(url, "_blank", "noopener"));
  const mint = options.mint ?? mintContentGrant;
  const roots = new Map<string, Promise<{ driveId: string; rootId: string } | null>>();
  const listings = new Map<string, { at: number; items: ItemRow[] }>();
  // The bytes already bought, per path: the version they are of, and the object
  // URL the transcript is showing. A new version replaces both.
  const bought = new Map<string, { etag: string; url: string }>();

  const root = (chatId: string): Promise<{ driveId: string; rootId: string } | null> => {
    let pending = roots.get(chatId);
    if (!pending) {
      pending = (async () => {
        const chat = await getChat(chatId);
        // The folder the agent works in, by the one rule the Files tab uses: a
        // workspace chat's files are in the workspace's shared tree, not under
        // the chat's own folder, so a link to them must be looked up there.
        const rootId = chatFilesRoot(chat, (chat as { working_node_id?: string | null }).working_node_id);
        if (!rootId) return null;
        const drive = await readJson<{ id: string }>("/api/v1/files/drives");
        return { driveId: drive.id, rootId };
      })().catch((err: unknown) => {
        roots.delete(chatId);
        throw err;
      });
      roots.set(chatId, pending);
    }
    return pending;
  };

  // The uploads folder's node, per chat, once found or made: every upload
  // after the first goes straight into it.
  const uploadFolders = new Map<string, Promise<string>>();

  /** The chat's working directory: the node Files names as where the chat's
   *  files live. A chat row that names none is its own. */
  const workingFolder = async (driveId: string, chatNode: string): Promise<string> => {
    const item = await readJson<{ object?: { metadata?: Record<string, unknown> } | null }>(
      `/api/v1/files/drives/${encodeURIComponent(driveId)}/items/${encodeURIComponent(chatNode)}`,
    );
    const target = item.object?.metadata?.files_node_id;
    return typeof target === "string" && target !== "" ? target : chatNode;
  };

  const uploadsFolder = (chatId: string, at: { driveId: string; rootId: string }): Promise<string> => {
    let pending = uploadFolders.get(chatId);
    if (!pending) {
      pending = (async () => {
        const working = await workingFolder(at.driveId, at.rootId);
        const existing = (rows: ItemRow[]) =>
          rows.find((row) => row.kind === "folder" && row.name === UPLOADS_FOLDER)?.id ?? null;
        const found = existing(await children(at.driveId, working, true));
        if (found) return found;
        const response = await apiFetch(
          apiUrl(
            `/api/v1/files/drives/${encodeURIComponent(at.driveId)}/items/${encodeURIComponent(working)}/children`,
          ),
          {
            method: "POST",
            credentials: "include",
            headers: { "content-type": "application/json", "Idempotency-Key": crypto.randomUUID() },
            body: JSON.stringify({ name: UPLOADS_FOLDER, kind: "folder", conflictBehavior: "fail" }),
          },
        );
        if (response.ok) return ((await response.json()) as ItemRow).id;
        // Another upload made it first.
        if (response.status === 409) {
          const raced = existing(await children(at.driveId, working, true));
          if (raced) return raced;
        }
        throw await failedResponse(response);
      })().catch((err: unknown) => {
        uploadFolders.delete(chatId);
        throw err;
      });
      uploadFolders.set(chatId, pending);
    }
    return pending;
  };

  const children = async (driveId: string, parentId: string, fresh: boolean): Promise<ItemRow[]> => {
    const cached = listings.get(parentId);
    if (cached && !fresh && now() - cached.at < ttl) return cached.items;
    const page = await readJson<ChildrenPage>(
      `/api/v1/files/drives/${encodeURIComponent(driveId)}/items/${encodeURIComponent(parentId)}/children`,
      { limit: 500 },
    );
    // A page with no rows in it is refused in words rather than walked: a
    // lookup that trips over the shape would reach the reader as a TypeError.
    if (!Array.isArray(page.value)) {
      throw new Error("the chat's folder listing came back without its rows");
    }
    listings.set(parentId, { at: now(), items: page.value });
    return page.value;
  };

  /** The node at `path` under the chat: each segment a child of the last; the
   *  first segment is also looked for one folder down, which is where the
   *  server puts what the chat is handed. The folder the node was listed from
   *  comes back with it — that is the containment the walk knows and the wire
   *  row need not carry. */
  const find = async (chatId: string, path: string, fresh: boolean): Promise<FoundNode | null> => {
    const at = await root(chatId);
    if (!at) return null;
    const segments = path.split("/");
    // A fresh lookup re-reads each folder once, not once per place it is
    // consulted: the root listing serves the direct walk and the folder scan.
    const reread = new Set<string>();
    const list = async (parent: string): Promise<ItemRow[]> => {
      const rows = await children(at.driveId, parent, fresh && !reread.has(parent));
      reread.add(parent);
      return rows;
    };
    const walk = async (from: string): Promise<FoundNode | null> => {
      let parent = from;
      let found: FoundNode | null = null;
      for (const segment of segments) {
        const rows = await list(parent);
        const row = rows.find((candidate) => candidate.name === segment) ?? null;
        if (!row) return null;
        found = {
          at: { nodeId: row.id, driveId: row.driveId, parentId: parent, name: row.name, path, kind: row.kind },
          etag: row.etag ?? "",
        };
        parent = row.id;
      }
      return found;
    };
    const direct = await walk(at.rootId);
    if (direct) return direct;
    const top = await list(at.rootId);
    for (const folder of top.filter((row) => row.kind === "folder")) {
      const below = await walk(folder.id);
      if (below) return below;
    }
    return null;
  };

  const lookup = async (chatId: string, path: string): Promise<FoundNode | null> =>
    (await find(chatId, path, false)) ?? (await find(chatId, path, true));

  const locate = async (chatId: string, path: string): Promise<ChatFileLocation | null> =>
    (await lookup(chatId, path))?.at ?? null;

  return {
    async upload(chatId, file, hint) {
      const at = await root(chatId);
      if (!at) throw new Error("this chat has no folder to upload into");
      const folder = await uploadsFolder(chatId, at);
      for (let draw = 0; draw < STAGE_NAME_ATTEMPTS; draw += 1) {
        const path = stagedFileName(hint.kind, hint.n, file.name);
        const renamed = new File([file], path.slice(path.lastIndexOf("/") + 1), { type: file.type });
        // The commit is create-only (`conflictBehavior: "fail"`, the client's
        // default). A name another upload already holds — an earlier message's
        // paste, or one racing this from another tab — is refused, the session
        // given back, and a new name drawn. Never "keep both": the server would
        // pick a name this path does not carry. Never "replace": the earlier
        // message would show these bytes.
        let taken = false;
        const clientOptions: UploadClientOptions = {
          // Without the drive the client has nowhere to read the queued commit
          // back from, and a commit refused under the drive lock (the name
          // taken between the check on the call and the commit) would read as
          // landed — with the path naming the OTHER upload's file.
          driveId: at.driveId,
          onProgress: (p) => hint.onProgress?.(Math.round((p.sent / Math.max(1, p.total)) * 100)),
          onConflict: () => {
            taken = true;
            return "skip";
          },
        };
        const client = options.uploads ? options.uploads(clientOptions) : new UploadClient(clientOptions);
        const handle = await client.start(renamed, folder);
        const finished = await handle.done();
        if (taken) continue;
        if (finished.state !== "done") {
          throw new Error(finished.error?.message ?? `the upload ${finished.state}`);
        }
        // The commit is queued server-side; the file is "uploaded" once it can
        // be found back, so the pill's check means what it says. When the
        // commit named the node it made, only that node counts.
        const made = finished.result?.nodeId ?? null;
        for (let look = 0; look < settle.attempts; look += 1) {
          const found = await find(chatId, path, true);
          if (found && (made === null || found.at.nodeId === made)) break;
          await new Promise((r) => setTimeout(r, settle.delayMs));
        }
        return { path };
      }
      throw new Error(`no free name for this upload after ${STAGE_NAME_ATTEMPTS} tries`);
    },
    async resolveUrl(chatId, path) {
      const found = await lookup(chatId, path);
      if (!found || found.at.kind !== "file") return null;
      const held = bought.get(path);
      // An etag the row does not carry says nothing about the bytes, so it is
      // never taken as "still the same file": the version is re-bought instead
      // of a stale image being shown of a file the agent has since rewritten.
      if (held && found.etag !== "" && held.etag === found.etag) return held.url;
      let grant: ContentGrant;
      try {
        grant = await mint({ driveId: found.at.driveId, itemId: found.at.nodeId, kind: "file" });
      } catch (error) {
        // The file is there but its bytes are still on the machine that wrote
        // it: "wait", which the image shows as loading and asks again when the
        // drive says the folder changed — not "nothing".
        if (isLivePending(error)) throw new ChatFileNotReady();
        return null;
      }
      try {
        const response = await fetch(grant.url, {
          mode: "cors",
          credentials: "omit",
          cache: "no-store",
        });
        if (!response.ok) return null;
        const url = URL.createObjectURL(await response.blob());
        // Only once the new bytes are in hand: a read that failed leaves what
        // the transcript is already showing alive.
        if (held) URL.revokeObjectURL(held.url);
        bought.set(path, { etag: found.etag, url });
        return url;
      } catch {
        return null;
      }
    },
    locate,
    openPath(chatId, path) {
      void locate(chatId, path).then((item) => {
        if (item && item.kind === "file") openUrl(viewerUrl(item.driveId, item.nodeId));
      });
    },
  };
}
