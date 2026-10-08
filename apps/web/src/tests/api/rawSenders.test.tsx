// Every request that does not go through the typed client still carries the
// session the typed client gives its own: the tab's org named on it, a 409
// `org_changed` turned into the org-changed reload, one retry after a
// `token_expired` refresh, and a refusal the server explained no further
// described to the reader without the path it went to or its status.
//
// Driven through each raw sender's public call with only `fetch` stubbed.

import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ORG_HEADER, SESSION_NOTICE_KEY, enterOrg, forgetActiveOrg, setOrgNavigator } from "@/api/activeOrg";
import { useDeleteChatTemplate } from "@/api/chatTemplates";
import { putChatWorkspace, useLinkAttachment } from "@/api/chats";
import { noteSessionExpiry, setUnauthorizedHandler } from "@/api/client";
import { getChat, setChatModel, stopTurn } from "@/api/cloudChat/transport";
import { ApiError } from "@/api/errors";
import { UploadClient } from "@/api/filesUpload";
import { createQueryClient } from "@/api/queryClient";
import { resetReadGate } from "@/api/readGate";
import { cloudChatFiles, mintContentGrant } from "@/pages/workspace/chat/data/chatFiles";
import { useSearchQuery } from "@/pages/workspace/files/useSearchQuery";
import { clearStorageMirror, safeSessionStorage } from "@alkera/ui/storage";

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CHAT = "0000000a-0000-4000-8000-00000000c4a7";
const NODE = "0000000b-0000-4000-8000-0000000000d1";
const TEMPLATE = "0000000c-0000-4000-8000-0000000000e2";
const DRIVE = "0000000d-0000-4000-8000-0000000000f3";
const REFRESH = "/api/v1/auth/refresh";
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** A mutation hook's `mutateAsync`, mounted on a real query client. */
function mutation<V>(useHook: () => { mutateAsync: (vars: V) => Promise<unknown> }, vars: V) {
  const client = createQueryClient({ retry: false });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useHook(), { wrapper });
  return result.current.mutateAsync(vars);
}

interface Sender {
  /** The request this sender makes, as `METHOD path`. */
  target: string;
  /** What the server answers it when it goes through. */
  answer: () => Response;
  /** The reads the sender makes before (or after) its own, answered as given, by `METHOD path`. */
  alongside?: Record<string, () => Response>;
  send: () => Promise<unknown>;
}

/** One files search, through the hook a page mounts: resolves with its rows once the query
 *  settles, or rejects with the query's error. */
async function searchOnce(): Promise<unknown> {
  const client = createQueryClient({ retry: false });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result, unmount } = renderHook(
    () => useSearchQuery({ driveId: DRIVE, folderId: undefined, scope: "drive", text: "quarterly", debounceMs: 0 }),
    { wrapper },
  );
  try {
    await waitFor(() => {
      const queries = client.getQueryCache().getAll();
      expect(queries.length).toBeGreaterThan(0);
      expect(queries.every((q) => q.state.status !== "pending" && q.state.fetchStatus === "idle")).toBe(true);
    });
    if (result.current.error) throw result.current.error;
    return result.current.items;
  } finally {
    unmount();
  }
}

const SENDERS: Record<string, Sender> = {
  "a chat read": {
    target: `GET /api/v1/chats/${CHAT}`,
    answer: () => json({ id: CHAT }),
    send: () => getChat(CHAT),
  },
  "a model switch": {
    target: `PUT /api/v1/chats/${CHAT}/model`,
    answer: () => json({ id: CHAT }),
    send: () => setChatModel(CHAT, "claude-opus-5-5"),
  },
  "a stop, which answers nothing": {
    target: `POST /api/v1/chats/${CHAT}/stop`,
    answer: () => new Response(null, { status: 202 }),
    send: () => stopTurn(CHAT),
  },
  "a workspace write": {
    target: `PUT /api/v1/chats/${CHAT}/workspace`,
    answer: () => json({ state: { tabs: [] }, updated_at: null }),
    send: () => putChatWorkspace({ chatId: CHAT, state: { tabs: [] } }),
  },
  "an attachment link": {
    target: `POST /api/v1/chats/${CHAT}/attachments`,
    answer: () => json({ nodeId: NODE, state: "available" }),
    send: () => mutation(useLinkAttachment, { chatId: CHAT, nodeId: NODE }),
  },
  "a template delete": {
    target: `DELETE /api/v1/chat-templates/${TEMPLATE}`,
    answer: () => new Response(null, { status: 204 }),
    send: () => mutation(useDeleteChatTemplate, { templateId: TEMPLATE }),
  },
  "a chat's folder read": {
    target: "GET /api/v1/files/drives",
    answer: () => json({ id: DRIVE }),
    alongside: {
      [`GET /api/v1/chats/${CHAT}`]: () => json({ id: CHAT, files_node_id: NODE }),
      [`GET /api/v1/files/drives/${DRIVE}/items/${NODE}/children`]: () => json({ value: [] }),
    },
    send: () => {
      const { locate } = cloudChatFiles();
      if (!locate) throw new Error("the browser's chat files port looks paths up");
      return locate(CHAT, "notes.txt");
    },
  },
  "a transcript image's grant": {
    target: `POST /api/v1/files/drives/${DRIVE}/items/${NODE}/content-grants`,
    answer: () => json({ url: "https://files.example/one-use", expiresAt: "2026-01-01T00:00:00Z" }),
    send: () => mintContentGrant({ driveId: DRIVE, itemId: NODE, kind: "file" }),
  },
  "a files search": {
    target: `GET /api/v1/files/drives/${DRIVE}/search`,
    answer: () => json({ value: [] }),
    send: searchOnce,
  },
  "an upload opening": {
    target: "POST /api/v1/files/uploads",
    answer: () =>
      json(
        {
          uploadId: "up-1",
          partSize: 4,
          partsTotal: 1,
          limits: { maxPartBytes: 4, maxParts: 1 },
          expiresAt: "2026-01-01T00:00:00Z",
        },
        201,
      ),
    send: async () => {
      const client = new UploadClient({ digest: async () => "d" });
      const handle = await client.start(new File(["abcd"], "a.txt"), NODE);
      // The run goes on to send its part; this test is about the opening only.
      await handle.cancel().catch(() => undefined);
      return handle;
    },
  },
};

const CASES = Object.entries(SENDERS);

let sent: Request[];
let navigated: string[];
let unauthorized: number;
let restoreNavigator: (url: string) => void;

const keyOf = (request: Request) => `${request.method} ${new URL(request.url).pathname}`;

/** Answer the sender's own request with `onTarget` (by attempt), the refresh
 *  with a renewal in the tab's org, and anything else with an empty success. */
function serve(sender: Sender, onTarget: (attempt: number) => Response) {
  const { target, alongside = {} } = sender;
  let attempt = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(input, init);
      sent.push(request);
      const key = keyOf(request);
      if (key === target) return onTarget(attempt++);
      const other = alongside[key];
      if (other) return other();
      if (key === `POST ${REFRESH}`) {
        return json({ expires_at: new Date(Date.now() + 10 * 60_000).toISOString(), user: { org_team_id: ORG_A } });
      }
      return new Response(null, { status: 204 });
    }),
  );
}

const toTarget = (target: string) => sent.filter((request) => keyOf(request) === target);

beforeEach(() => {
  sent = [];
  navigated = [];
  unauthorized = 0;
  forgetActiveOrg();
  clearStorageMirror();
  safeSessionStorage().remove(SESSION_NOTICE_KEY);
  noteSessionExpiry(null);
  resetReadGate();
  restoreNavigator = setOrgNavigator((url) => navigated.push(url));
  setUnauthorizedHandler(() => {
    unauthorized += 1;
  });
});

afterEach(() => {
  setOrgNavigator(restoreNavigator);
  setUnauthorizedHandler(null);
  forgetActiveOrg();
  noteSessionExpiry(null);
  vi.unstubAllGlobals();
});

describe.each(CASES)("%s", (_name, sender) => {
  it("names the org this tab rendered", async () => {
    enterOrg(ORG_A);
    serve(sender, () => sender.answer());
    await sender.send();
    const [request] = toTarget(sender.target);
    expect(request?.headers.get(ORG_HEADER)).toBe(ORG_A);
  });

  it("reloads with the org-changed notice on a 409 org_changed, and never retries", async () => {
    enterOrg(ORG_A);
    serve(sender, () =>
      json({ error: { code: "org_changed", message: "You switched organizations in another window." } }, 409),
    );
    await expect(sender.send()).rejects.toBeInstanceOf(ApiError);
    expect(navigated).toEqual(["/"]);
    expect(toTarget(sender.target)).toHaveLength(1);
    expect(unauthorized).toBe(0);
  });

  it("refreshes once after a token_expired and sends again", async () => {
    enterOrg(ORG_A);
    serve(sender, (attempt) =>
      attempt === 0
        ? json({ error: { code: "token_expired", message: "expired" } }, 401)
        : sender.answer(),
    );
    await sender.send();
    expect(toTarget(sender.target)).toHaveLength(2);
    expect(sent.filter((request) => keyOf(request) === `POST ${REFRESH}`)).toHaveLength(1);
    expect(unauthorized).toBe(0);
  });

  it("hands any other 401 to the sign-out", async () => {
    serve(sender, () => json({ error: { code: "session_revoked", message: "gone" } }, 401));
    await expect(sender.send()).rejects.toBeInstanceOf(ApiError);
    expect(toTarget(sender.target)).toHaveLength(1);
    expect(unauthorized).toBe(1);
  });

  it.each([404, 409, 429, 500, 502])(
    "describes a %i the server explained no further without its path or status",
    async (status) => {
      serve(sender, () => new Response("upstream said no", { status }));
      const error: unknown = await sender.send().then(
        () => null,
        (thrown: unknown) => thrown,
      );
      expect(error).toBeInstanceOf(ApiError);
      const { message } = error as ApiError;
      expect(message).not.toMatch(/\/api\//);
      expect(message).not.toMatch(UUID);
      expect(message).not.toMatch(new RegExp(`\\b${status}\\b`));
      expect(message).not.toBe("");
      expect((error as ApiError).status).toBe(status);
    },
  );
});

describe("a refusal the server explained", () => {
  it.each(CASES)("keeps the server's own sentence on %s", async (_name, sender) => {
    serve(sender, () => json({ error: { code: "nope", message: "That chat is archived." } }, 422));
    await expect(sender.send()).rejects.toMatchObject({ message: "That chat is archived.", status: 422 });
  });
});
