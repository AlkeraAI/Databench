// The page a chat template IS, against a stubbed server.
//
// A template is a row (the title, the brief, the provenance) AND a folder (the
// files a new chat opens with), and the page is the only surface that has both
// in hand. So what is pinned here is the seam between them: the brief is edited
// against the version it was read at, the two ways out go to the two different
// nodes the server named, sharing addresses the folder rather than the row, and
// a template this reader may not have says so instead of rendering an empty
// shell.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { ChatTemplatePage } from "@/pages/workspace/templates/ChatTemplatePage";

const TEMPLATE_ID = "tpl_4";
const DRIVE = "dr_1";
/** The template's own folder — what `files_node_id` names, what a new chat is
 *  started from, and what a share is made over. */
const TEMPLATE_NODE = "nd_tpl";
/** The working folder inside it — what "Browse files" lists. A different node,
 *  deliberately: a page that conflated the two would pass every assertion below
 *  with one id. */
const SCRATCH_NODE = "nd_scratch";

interface Call {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

let calls: Call[] = [];
/** The row the server answers a read with. Mutated by a write so a refetch sees
 *  what the write left behind. */
let row: Record<string, unknown>;
/** What a read of the row answers; 200 unless a test says otherwise. */
let readStatus = 200;
/** Queued answers for the PUT, consumed in order; an exhausted queue means "the
 *  write lands". */
let putAnswers: { status: number; body: unknown }[] = [];
let deleteStatus = 204;
/** What the template's folder says about itself — the working folder it names. */
let nodeFacet: Record<string, unknown>;
/** Whether this reader holds the edit rung on the template's folder. */
let nodeWritable = true;

function freshRow(): Record<string, unknown> {
  return {
    id: TEMPLATE_ID,
    title: "Monthly revenue",
    version: 3,
    owner_user_id: "usr_dana",
    created_at: "2026-02-01T09:00:00Z",
    updated_at: "2026-02-09T17:30:00Z",
    files_node_id: TEMPLATE_NODE,
    brief: "Pull last month's revenue by region.",
    model: null,
    permission_mode: "read_only",
    source_chat_id: "cht_7",
    saved_from_seq: 42,
  };
}

function node(): Record<string, unknown> {
  return {
    id: TEMPLATE_NODE,
    driveId: DRIVE,
    kind: "folder",
    name: "Monthly revenue.alkerachat.template",
    nameDisplay: "Monthly revenue.alkerachat.template",
    parentId: "nd_templates",
    etag: "et_1",
    capabilities: {
      can_read: true,
      can_write: nodeWritable,
      can_share: true,
      refusals: {},
    },
    object: {
      type: "chat_template",
      id: TEMPLATE_ID,
      title: "Monthly revenue",
      web_url: `/templates/${TEMPLATE_ID}`,
      ...nodeFacet,
    },
  };
}

function stub(): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // The Files hooks go out through the typed client, which hands `fetch` a
      // built Request; the template hooks send a plain url + init. Both shapes
      // have to be read or half the writes below record as GETs with no body.
      const asRequest = input instanceof Request ? input : null;
      const raw = asRequest ? asRequest.url : String(input);
      const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
      const headers = Object.fromEntries(
        new Headers((init?.headers as HeadersInit | undefined) ?? asRequest?.headers).entries(),
      );
      let body: unknown = null;
      if (typeof init?.body === "string") body = JSON.parse(init.body);
      else if (asRequest && method !== "GET") {
        const text = await asRequest.clone().text();
        body = text === "" ? null : JSON.parse(text);
      }
      calls.push({ method, url: raw, headers, body });
      const url = new URL(raw, "http://localhost");
      const answer = (status: number, payload: unknown): Response =>
        new Response(status === 204 || payload === null ? null : JSON.stringify(payload), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname.startsWith("/api/v1/chat-templates/")) {
        if (method === "PUT") {
          const queued = putAnswers.shift();
          if (queued && queued.status >= 300) return answer(queued.status, queued.body);
          const written = body as { title?: string; brief?: string };
          row = {
            ...row,
            ...(written.title === undefined ? {} : { title: written.title }),
            ...(written.brief === undefined ? {} : { brief: written.brief }),
            version: (row.version as number) + 1,
          };
          return answer(200, row);
        }
        if (method === "DELETE") return answer(deleteStatus, null);
        if (readStatus >= 300) {
          return answer(readStatus, { detail: { code: "not_found", message: "" } });
        }
        return answer(200, row);
      }
      if (url.pathname === "/api/v1/files/drives") {
        return answer(200, { id: DRIVE, orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.pathname.endsWith("/permissions")) return answer(200, { value: [] });
      if (url.pathname.includes("/items/")) return answer(200, node());
      if (url.pathname.endsWith("/org/members")) return answer(200, []);
      if (url.pathname.endsWith("/teams")) return answer(200, []);
      return answer(200, {});
    }),
  );
}

/** Where the router is, read from inside the router the page renders in. */
function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

function mount(
  at = `/templates/${TEMPLATE_ID}`,
  // The app's own retry ladder is what decides whether a refusal settles or is
  // asked three more times, so a test about that half passes `retryDelay: 0`
  // rather than switching retries off and pinning nothing.
  queryDefaults: Parameters<typeof createQueryClient>[0] = { retry: false },
) {
  return render(
    <QueryClientProvider client={createQueryClient(queryDefaults)}>
      <MemoryRouter initialEntries={[at]}>
        <Where />
        <Routes>
          <Route path="/templates/:templateId" element={<ChatTemplatePage />} />
          <Route path="/chat" element={<p>a new chat</p>} />
          <Route path="/files" element={<p>the drive</p>} />
          <Route path="/files/:nodeId" element={<p>a folder listing</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The template's own writes, in the order they went out. */
function writes(method: string): Call[] {
  return calls.filter(
    (call) => call.method === method && call.url.includes(`/chat-templates/${TEMPLATE_ID}`),
  );
}

beforeEach(() => {
  row = freshRow();
  readStatus = 200;
  putAnswers = [];
  deleteStatus = 204;
  nodeFacet = { metadata: { files_node_id: SCRATCH_NODE } };
  nodeWritable = true;
  stub();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("what the page says a template is", () => {
  it("names it, shows the brief its author wrote and the facts behind it", async () => {
    mount();

    expect(await screen.findByRole("heading", { level: 1, name: "Monthly revenue" })).toBeTruthy();
    expect(
      await screen.findByDisplayValue("Pull last month's revenue by region."),
    ).toBeTruthy();
    // The facts are the provenance a reader needs before starting from it: when
    // it was saved, when it last changed, and the stance a chat from it runs in.
    const facts = screen.getByRole("group", { name: "Facts" });
    expect(within(facts).getByText("Read-only")).toBeTruthy();
    expect(within(facts).getByText(/Feb 1, 2026/)).toBeTruthy();
    expect(within(facts).getByText(/Feb 9, 2026/)).toBeTruthy();
  });

  it("says so when the brief is empty rather than showing a blank panel", async () => {
    row = { ...freshRow(), brief: "" };
    mount();

    await screen.findByRole("heading", { level: 1, name: "Monthly revenue" });
    expect(screen.getByPlaceholderText(/no brief/i)).toBeTruthy();
  });

  it("shows the brief without an editor to a reader who may not write the folder", async () => {
    // The edit rung lives on the template's folder, so a reader holding only
    // READ gets the text and no field to type in.
    nodeWritable = false;
    stub();
    mount();

    // The brief as prose, not as the contents of a field.
    expect(
      await screen.findByText("Pull last month's revenue by region.", { selector: "p" }),
    ).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "Brief" })).toBeNull();
    expect(screen.queryByRole("button", { name: /save brief/i })).toBeNull();
    // The name rides the same rung as the brief: both are the template's own row.
    expect(screen.queryByRole("button", { name: "Rename" })).toBeNull();
  });
});

describe("renaming a template", () => {
  it("sends the new name fenced on the version it read", async () => {
    const person = userEvent.setup();
    mount();

    await person.click(await screen.findByRole("button", { name: "Rename" }));
    const field = screen.getByRole("textbox", { name: "Name" });
    await person.clear(field);
    await person.type(field, "Quarterly revenue{Enter}");

    await waitFor(() => expect(writes("PUT")).toHaveLength(1));
    expect(writes("PUT")[0]?.body).toEqual({
      title: "Quarterly revenue",
      expected_version: 3,
    });
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Quarterly revenue"),
    );
  });

  it("writes nothing when the name comes back unchanged", async () => {
    const person = userEvent.setup();
    mount();

    await person.click(await screen.findByRole("button", { name: "Rename" }));
    await person.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1, name: "Monthly revenue" })).toBeTruthy(),
    );
    expect(writes("PUT")).toHaveLength(0);
  });

  it("keeps the name on screen and says why when the server refuses", async () => {
    putAnswers = [
      {
        status: 403,
        body: {
          detail: {
            code: "chat_template.write_rung_required",
            message: "You need edit access on this template's folder",
          },
        },
      },
    ];
    const person = userEvent.setup();
    mount();

    await person.click(await screen.findByRole("button", { name: "Rename" }));
    const field = screen.getByRole("textbox", { name: "Name" });
    await person.clear(field);
    await person.type(field, "Quarterly revenue{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You need edit access on this template's folder",
    );
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Quarterly revenue");
  });
});

describe("editing the brief", () => {
  it("sends the version it read, so a stale edit cannot overwrite silently", async () => {
    const person = userEvent.setup();
    mount();

    const field = await screen.findByRole("textbox", { name: "Brief" });
    await person.clear(field);
    await person.type(field, "Ask for the region first.");
    await person.click(screen.getByRole("button", { name: /save brief/i }));

    await waitFor(() => expect(writes("PUT")).toHaveLength(1));
    expect(writes("PUT")[0]?.body).toEqual({
      brief: "Ask for the region first.",
      expected_version: 3,
    });
  });

  it("reads the template again after a conflict, so the next save is fenced on the new version", async () => {
    putAnswers = [
      {
        status: 409,
        body: {
          detail: {
            code: "version_conflict",
            message: "This template changed since you read it; read it again and retry",
          },
        },
      },
    ];
    const person = userEvent.setup();
    mount();

    const field = await screen.findByRole("textbox", { name: "Brief" });
    await person.clear(field);
    await person.type(field, "Ask for the region first.");
    // Somebody else lands an edit between the read and the save: the row the
    // re-read finds is two versions on.
    row = { ...row, version: 5, brief: "Theirs." };
    await person.click(screen.getByRole("button", { name: /save brief/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/changed/i);
    // The typed text survives the refusal — losing it is the thing a person
    // cannot recover from.
    expect(screen.getByRole("textbox", { name: "Brief" })).toHaveValue(
      "Ask for the region first.",
    );

    await person.click(screen.getByRole("button", { name: /save brief/i }));
    await waitFor(() => expect(writes("PUT")).toHaveLength(2));
    expect(writes("PUT")[1]?.body).toEqual({
      brief: "Ask for the region first.",
      expected_version: 5,
    });
  });
});

describe("the ways out of a template", () => {
  it("starts a new chat from the template's own folder", async () => {
    const person = userEvent.setup();
    mount();

    await person.click(await screen.findByRole("button", { name: "New chat" }));

    await waitFor(() =>
      expect(screen.getByTestId("where")).toHaveTextContent(`/chat?source=${TEMPLATE_NODE}`),
    );
  });

  it("lists the working folder the server named, not the template folder", async () => {
    const person = userEvent.setup();
    mount();

    await person.click(await screen.findByRole("button", { name: "Browse files" }));

    await waitFor(() =>
      expect(screen.getByTestId("where")).toHaveTextContent(`/files/${SCRATCH_NODE}`),
    );
    expect(screen.getByText("a folder listing")).toBeTruthy();
  });

  it("lists the template folder itself when no working folder is named", async () => {
    nodeFacet = {};
    const person = userEvent.setup();
    mount();

    await person.click(await screen.findByRole("button", { name: "Browse files" }));

    await waitFor(() =>
      expect(screen.getByTestId("where")).toHaveTextContent(`/files/${TEMPLATE_NODE}`),
    );
  });

  it("shares the template's folder, titled with the template's name", async () => {
    const person = userEvent.setup();
    mount();

    await person.click(await screen.findByRole("button", { name: "Share…" }));

    // The dialog reads the node it was handed; a page that passed the row's id
    // would open the dialog over something the drive has never heard of.
    expect(await screen.findByText("Share “Monthly revenue”")).toBeTruthy();
    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.method === "GET" &&
            call.url.includes(`/items/${TEMPLATE_NODE}/permissions`),
        ),
      ).toBe(true),
    );
  });
});

describe("deleting a template", () => {
  it("asks first, then takes it away and leaves for the drive", async () => {
    const person = userEvent.setup();
    mount();

    await person.click(await screen.findByRole("button", { name: "Delete…" }));
    // Nothing is gone yet: the question is the point of the step.
    expect(writes("DELETE")).toHaveLength(0);

    const dialog = await screen.findByRole("dialog");
    await person.click(within(dialog).getByRole("button", { name: "Delete template" }));

    await waitFor(() => expect(writes("DELETE")).toHaveLength(1));
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/files"));
  });

  // The question opens with Cancel under the finger, so the key a reader presses on the way past
  // backs out rather than taking the template from everyone it is shared with.
  it("opens on Cancel, and neither Enter nor a dismissal deletes anything", async () => {
    const person = userEvent.setup();
    mount();

    await person.click(await screen.findByRole("button", { name: "Delete…" }));
    const dialog = await screen.findByRole("dialog");
    const cancel = within(dialog).getByRole("button", { name: "Cancel" });
    await waitFor(() => expect(cancel).toHaveFocus());
    await person.keyboard("{Enter}");
    expect(writes("DELETE")).toHaveLength(0);

    await person.click(await screen.findByRole("button", { name: "Delete…" }));
    await person.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(writes("DELETE")).toHaveLength(0);
    expect(screen.getByTestId("where")).toHaveTextContent(`/templates/${TEMPLATE_ID}`);
  });

  it("keeps the reader on the page and says why when the server refuses", async () => {
    deleteStatus = 403;
    const person = userEvent.setup();
    mount();

    await person.click(await screen.findByRole("button", { name: "Delete…" }));
    const dialog = await screen.findByRole("dialog");
    await person.click(within(dialog).getByRole("button", { name: "Delete template" }));

    await waitFor(() => expect(writes("DELETE")).toHaveLength(1));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByTestId("where")).toHaveTextContent(`/templates/${TEMPLATE_ID}`);
  });
});

describe("a template that is not this reader's to see", () => {
  it("says so once, instead of an empty page or a spinner that never stops", async () => {
    readStatus = 404;
    mount();

    expect(
      await screen.findByRole("heading", { name: /isn['’]t here, or isn['’]t shared with you/i }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "New chat" })).toBeNull();
  });

  it("says the same thing for a refusal as for a template that does not exist", async () => {
    readStatus = 403;
    mount();

    expect(
      await screen.findByRole("heading", { name: /isn['’]t here, or isn['’]t shared with you/i }),
    ).toBeTruthy();
  });

  it("offers a retry when the read FAILED rather than answered", async () => {
    // A server that broke has told the reader nothing about whether the template
    // exists or is theirs, so the page must not say either — it says the read
    // failed and offers to ask again.
    readStatus = 500;
    mount(`/templates/${TEMPLATE_ID}`, { retryDelay: 0 });

    expect(await screen.findByRole("heading", { name: /could not be loaded/i })).toBeTruthy();
    expect(screen.queryByText(/isn['’]t here/i)).toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });
});
