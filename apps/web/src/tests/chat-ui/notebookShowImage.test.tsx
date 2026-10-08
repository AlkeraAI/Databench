// An image a notebook's cell shows, put in the chat by `notebook.show_output`.
//
// The tool's reply names the image by its hash; the card reads the bytes only
// once it is on screen, draws them inline, and says plainly why when it has
// none to draw: a reader the notebook is not open to, an output the cell no
// longer shows, a shell with nowhere to read it from. The last block drives
// the card through the chat's own reader against a stubbed blob route, so the
// whole path a reader's browser takes is covered.

import type { StepEnvironment, StoredOutputRead } from "@alkera/ui";
import { QueryClientProvider } from "@tanstack/react-query";
import { render, renderHook, waitFor, within } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { installChatRuntime, resetChatRuntime, type ChatDataSource, type ChatHost } from "@/pages/workspace/chat/data";
import type { ChatFileLocation } from "@/pages/workspace/chat/data/chatFiles";
import { useNotebookImage } from "@/pages/workspace/chat/notebookImages";

import { renderBody, stepOf, toolPart } from "./_steps";
import { handScrolledViewport } from "./_viewport";

const CHAT = "11111111-1111-1111-1111-111111111111";
const PATH = "geo/geo-overview.alknb.py";
const SHA = "50c4fb6e076d592e5359af9d1549d46d3afa268daacd09a7114c161dfb256048";
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

const IMAGE = toolPart("alkera_notebook.show_output", {
  input: { path: PATH, cell: "chart_sov_vs_sentiment" },
  output: JSON.stringify({
    path: PATH,
    cell_id: "a7yg9x7evz",
    cell_name: "chart_sov_vs_sentiment",
    kind: "image",
    available: ["image"],
    image: { index: 0, total: 1, mime: "image/png", bytes: 44769, sha256: SHA },
    note: "",
  }),
});

type ReadImage = NonNullable<StepEnvironment["notebookImage"]>;

let viewport: ReturnType<typeof handScrolledViewport>;
const made: Blob[] = [];

beforeEach(() => {
  viewport = handScrolledViewport();
  made.length = 0;
  // jsdom has no object URLs: each one names the blob it was made for.
  URL.createObjectURL = vi.fn((blob: Blob) => {
    made.push(blob);
    return `blob:shown/${made.length}`;
  });
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetChatRuntime();
});

function ready(): ReadImage {
  return vi.fn(async (): Promise<StoredOutputRead<Blob>> => ({ kind: "ready", value: new Blob([PNG], { type: "image/png" }) }));
}

describe("a notebook image in the chat", () => {
  it("is drawn inline from the bytes its hash names, once the card is on screen", async () => {
    const notebookImage = ready();
    const body = renderBody(stepOf(IMAGE, { notebookImage }));

    viewport.scrollTo(body);

    const image = await within(body).findByRole("img", { name: "chart_sov_vs_sentiment image" });
    expect(notebookImage).toHaveBeenCalledWith(PATH, SHA);
    expect(image.getAttribute("src")).toBe("blob:shown/1");
    expect(new Uint8Array(await made[0].arrayBuffer())).toEqual(PNG);
    expect(body.textContent).not.toContain("Open the notebook");
  });

  it("reads nothing until the card is on screen, and says it is loading meanwhile", async () => {
    const notebookImage = ready();
    const body = renderBody(stepOf(IMAGE, { notebookImage }));

    expect(within(body).getByRole("status", { name: "chart_sov_vs_sentiment image" }).textContent).toBe("Loading the image.");
    expect(viewport.watched()).toHaveLength(1);
    expect(notebookImage).not.toHaveBeenCalled();
    expect(body.querySelector("img")).toBeNull();

    viewport.scrollTo(body);

    await within(body).findByRole("img", { name: "chart_sov_vs_sentiment image" });
    expect(notebookImage).toHaveBeenCalledTimes(1);
  });

  it("lets go of the image's object URL when the card leaves", async () => {
    const step = stepOf(IMAGE, { notebookImage: ready() });
    const { unmount, container } = renderWithUnmount(step.body);
    viewport.scrollTo(container);
    await within(container).findByRole("img");

    unmount();

    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:shown/1");
  });

  it.each([
    ["a reader the notebook is not open to", { kind: "refused" } as const, "You don't have access to this notebook."],
    [
      "an image the cell no longer shows",
      { kind: "gone" } as const,
      "This image is no longer in the notebook. The cell ran again or was deleted.",
    ],
  ])("says so for %s, and shows no image", async (_case, answer, note) => {
    const body = renderBody(stepOf(IMAGE, { notebookImage: vi.fn(async () => answer) }));
    viewport.scrollTo(body);

    expect(await within(body).findByText(note)).toBeTruthy();
    expect(body.querySelector("img")).toBeNull();
  });

  it("says it could not load a read that failed, never that access was refused", async () => {
    const body = renderBody(stepOf(IMAGE, { notebookImage: vi.fn(async () => Promise.reject(new Error("offline"))) }));
    viewport.scrollTo(body);

    expect(await within(body).findByText("The image could not be loaded.")).toBeTruthy();
    expect(body.textContent).not.toContain("access");
  });

  it("says to open the notebook in a shell with nowhere to read images from", () => {
    const body = renderBody(stepOf(IMAGE));

    expect(within(body).getByText("Open the notebook to see this image.")).toBeTruthy();
    expect(viewport.watched()).toHaveLength(0);
  });
});

describe("a notebook image read through the chat's own reader", () => {
  function located(): ChatFileLocation {
    return { nodeId: "nb-node", driveId: "drive-1", parentId: "root", name: "geo-overview.alknb.py", path: PATH, kind: "file" };
  }

  function reader(locate: (chatId: string, path: string) => Promise<ChatFileLocation | null>): ReadImage {
    installChatRuntime({ source: { chatFiles: { locate } } as unknown as ChatDataSource, host: {} as unknown as ChatHost });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: createQueryClient({ retry: false }) }, children);
    const { result } = renderHook(() => useNotebookImage(CHAT), { wrapper });
    if (!result.current) throw new Error("the chat offered no image reader");
    return result.current;
  }

  function blobRoute(status: number) {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(status === 200 ? PNG : "{}", { status }));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("draws the bytes the notebook's blob route answers", async () => {
    const fetchMock = blobRoute(200);
    const body = renderBody(stepOf(IMAGE, { notebookImage: reader(async () => located()) }));
    viewport.scrollTo(body);

    await within(body).findByRole("img", { name: "chart_sov_vs_sentiment image" });
    expect(String(fetchMock.mock.calls[0][0])).toMatch(new RegExp(`/api/v1/notebooks/drive-1/nb-node/blobs/${SHA}$`));
    expect(new Uint8Array(await made[0].arrayBuffer())).toEqual(PNG);
  });

  it.each([
    ["the route refuses the reader", async () => located(), 403],
    ["no notebook the reader may open is at the path", async () => null, 200],
  ])("shows a reader without access the refusal, not the image, when %s", async (_case, locate, status) => {
    blobRoute(status);
    const body = renderBody(stepOf(IMAGE, { notebookImage: reader(locate) }));
    viewport.scrollTo(body);

    expect(await within(body).findByText("You don't have access to this notebook.")).toBeTruthy();
    expect(body.querySelector("img")).toBeNull();
  });

  it("says the image is gone when the route no longer finds it", async () => {
    blobRoute(404);
    const body = renderBody(stepOf(IMAGE, { notebookImage: reader(async () => located()) }));
    viewport.scrollTo(body);

    await waitFor(() =>
      expect(body.textContent).toContain("This image is no longer in the notebook. The cell ran again or was deleted."),
    );
    expect(body.querySelector("img")).toBeNull();
  });
});

function renderWithUnmount(node: ReactNode) {
  const { container, unmount } = render(createElement("div", { className: "chat-root" }, node));
  return { container, unmount };
}
