import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesActions } from "@/pages/workspace/files/FilesActions";
import { SaveAsTemplateDialog } from "@/pages/workspace/files/SaveAsTemplateDialog";
import { expectNoRawFailureText, serverFellOver } from "@/tests/fixtures/rawFailures";

// Saving a chat as a template is a copy of the chat's files, so the request has
// to be exactly right once: the chat OBJECT is what the route takes (not the
// node the row is), the attempt carries an idempotency key so a retry cannot
// leave two templates behind, and the two refusals a person can actually
// provoke — a chat with no files, a drive with no room — have to arrive as
// sentences rather than as a dialog that will not close.

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "notes.txt",
    nameDisplay: "notes.txt",
    etag: "et_1",
    capabilities: { can_read: true, can_write: true, can_share: true, can_delete: true },
    ...over,
  } as Item;
}

const CHAT = item({
  id: "nd_chat",
  kind: "folder",
  name: "3952c9e2.alkerachat",
  nameDisplay: "3952c9e2.alkerachat",
  object: { type: "chat", id: "cht_9", title: "Q3 review", web_url: "/chat/cht_9" },
} as unknown as Partial<Item>);

const TEMPLATE = item({
  id: "nd_tpl",
  kind: "folder",
  name: "Monthly revenue.alkerachat.template",
  nameDisplay: "Monthly revenue.alkerachat.template",
  object: {
    type: "chat_template",
    id: "tpl_4",
    title: "Monthly revenue",
    web_url: "/templates/tpl_4",
  },
} as unknown as Partial<Item>);

const MADE = {
  id: "tpl_new",
  title: "Q3 review",
  version: 1,
  brief: "Pull Q3 revenue by region.",
  files_node_id: "nd_tpl_scratch",
  source_chat_id: "cht_9",
};

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status < 400,
    status,
    json: async () => body,
  } as unknown as Response;
}

function mount(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async () => jsonResponse(201, MADE));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function fillAndSave() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Save template" }));
  return user;
}

describe("the Save as template dialog", () => {
  it("posts the chat OBJECT id, not the node the row is", async () => {
    mount(<SaveAsTemplateDialog open chat={CHAT} onClose={vi.fn()} />);
    await fillAndSave();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/api/v1/chat-templates");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ source_chat_id: "cht_9" });
  });

  it("carries an Idempotency-Key, so a replayed attempt cannot copy the files twice", async () => {
    mount(<SaveAsTemplateDialog open chat={CHAT} onClose={vi.fn()} />);
    await fillAndSave();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toMatch(/\S/);
  });

  it("sends the name and the brief the person typed", async () => {
    mount(<SaveAsTemplateDialog open chat={CHAT} onClose={vi.fn()} />);
    const user = userEvent.setup();
    await user.clear(screen.getByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Monthly revenue");
    await user.type(screen.getByLabelText("Brief"), "Ask for the month first.");
    await user.click(screen.getByRole("button", { name: "Save template" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      source_chat_id: "cht_9",
      title: "Monthly revenue",
      brief: "Ask for the month first.",
    });
  });

  it("reports the template it made and closes", async () => {
    const onSaved = vi.fn();
    const onClose = vi.fn();
    mount(<SaveAsTemplateDialog open chat={CHAT} onClose={onClose} onSaved={onSaved} />);
    await fillAndSave();

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(MADE));
    expect(onClose).toHaveBeenCalled();
  });

  it("says so when the chat has no files to save, and stays open", async () => {
    const onClose = vi.fn();
    fetchMock.mockResolvedValue(
      jsonResponse(422, { code: "chat.no_working_directory", message: "no working directory" }),
    );
    mount(<SaveAsTemplateDialog open chat={CHAT} onClose={onClose} />);
    await fillAndSave();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This chat has no files yet, so there is nothing to save as a template.",
    );
    expect(onClose).not.toHaveBeenCalled();
  });

  it("says a server that fell over in its own sentence, never in the server's words, and stays open", async () => {
    const onClose = vi.fn();
    fetchMock.mockImplementation(async () => serverFellOver());
    mount(<SaveAsTemplateDialog open chat={CHAT} onClose={onClose} />);
    await fillAndSave();

    expect(await screen.findByRole("alert")).toHaveTextContent("That did not go through.");
    expectNoRawFailureText();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("is built from the UI library's fields and callout", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { code: "chat.no_working_directory", message: "no working directory" }),
    );
    mount(<SaveAsTemplateDialog open chat={CHAT} onClose={vi.fn()} />);
    const name = screen.getByLabelText("Name");
    const brief = screen.getByLabelText("Brief");
    expect(name).toHaveClass("alk-input");
    expect(name.tagName).toBe("INPUT");
    expect(brief).toHaveClass("alk-input");
    expect(brief.tagName).toBe("TEXTAREA");
    await fillAndSave();
    expect(await screen.findByRole("alert")).toHaveClass("alk-callout");
  });

  it.each([
    [
      "the org is full",
      "files.quota_bytes",
      "Your organization is out of storage, so the template's files could not be copied.",
    ],
    [
      "the person is full",
      "files.user_quota_bytes",
      "You are out of storage, so the template's files could not be copied. Ask your org or team admin for more room.",
    ],
  ])("says so when %s", async (_label, code, sentence) => {
    fetchMock.mockResolvedValue(jsonResponse(507, { code, message: "no room" }));
    mount(<SaveAsTemplateDialog open chat={CHAT} onClose={vi.fn()} />);
    await fillAndSave();

    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
  });
});

describe("Save as template… from the row menu", () => {
  function surface(props: Partial<React.ComponentProps<typeof FilesActions>> = {}) {
    return mount(
      <FilesActions canWriteHere driveId="dr_1" currentFolderId="nd_home" selection={[CHAT]} {...props}>
        {(api) => (
          <button type="button" onClick={() => api.run("save-as-template")}>
            fire
          </button>
        )}
      </FilesActions>,
    );
  }

  it("opens the dialog over the chat the row names", async () => {
    surface();
    await userEvent.setup().click(screen.getByRole("button", { name: "fire" }));
    expect(await screen.findByRole("textbox", { name: "Name" })).toHaveValue("Q3 review");
  });

  it("hands the row to a surface that brought its own dialog", async () => {
    const onSaveAsTemplate = vi.fn();
    surface({ onSaveAsTemplate });
    await userEvent.setup().click(screen.getByRole("button", { name: "fire" }));
    expect(onSaveAsTemplate).toHaveBeenCalledWith(CHAT);
    expect(screen.queryByRole("textbox", { name: "Name" })).toBeNull();
  });

  it("does nothing on a row that is not a chat", async () => {
    const onSaveAsTemplate = vi.fn();
    surface({ selection: [TEMPLATE], onSaveAsTemplate });
    await userEvent.setup().click(screen.getByRole("button", { name: "fire" }));
    expect(onSaveAsTemplate).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox", { name: "Name" })).toBeNull();
  });
});

describe("New chat from template from the row menu", () => {
  function surface(
    selection: readonly Item[],
    props: Partial<React.ComponentProps<typeof FilesActions>> = {},
  ) {
    return mount(
      <FilesActions canWriteHere driveId="dr_1" currentFolderId="nd_home" selection={selection} {...props}>
        {(api) => (
          <button type="button" onClick={() => api.run("new-chat-from-template")}>
            fire
          </button>
        )}
      </FilesActions>,
    );
  }

  it("hands the template over to the surface that owns chats", async () => {
    const onNewChatFromTemplate = vi.fn();
    surface([TEMPLATE], { onNewChatFromTemplate });
    await userEvent.setup().click(screen.getByRole("button", { name: "fire" }));
    expect(onNewChatFromTemplate).toHaveBeenCalledWith(TEMPLATE);
  });

  it("does nothing on a chat row: a chat does not start a chat", async () => {
    const onNewChatFromTemplate = vi.fn();
    surface([CHAT], { onNewChatFromTemplate });
    await userEvent.setup().click(screen.getByRole("button", { name: "fire" }));
    expect(onNewChatFromTemplate).not.toHaveBeenCalled();
  });
});
