// The composer never invents a model. On a new chat it renders the live gateway
// catalog; a catalog that comes back empty or errored says so in a banner and
// leaves the chip off, rather than showing a plausible name nobody offered.
//
// The banner is dismissable, and dismissing it silences only the problem the
// user just read: a later, distinct failure has to raise it again.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const { ds, refetchEmpty } = vi.hoisted(() => {
  const ds = {
    // No chats: the surface stays on the NEW-chat path, where an empty catalog
    // is a problem worth reporting.
    listChats: vi.fn(async () => [] as unknown[]),
    listModels: vi.fn(async () => [] as unknown[]),
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: async () => [] as unknown[],
    listContext: async () => ({ total: 0, items: [] as unknown[] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] as unknown[] }),
    getChatTurns: vi.fn(async () => [] as unknown[]),
    searchFiles: async () => [] as unknown[],
    subscribeChat: () => () => {},
    subscribePermissionMode: () => () => {},
    getPermissionMode: async () => "default",
    setPermissionMode: vi.fn(async () => {}),
    setEffort: vi.fn(async () => {}),
    // Never resolves: holds the surface in the creating state so a first paint
    // is assertable without a navigation race.
    createChat: vi.fn(() => new Promise<never>(() => {})),
    sendUserMessage: vi.fn(async () => ({})),
  };
  // The models query's refetchInterval — off by default; the recovery tests set
  // a fast poll so the catalog self-heals at test speed.
  const refetchEmpty = vi.fn((_query?: { state: { data?: unknown; status?: string } }): number | false => false);
  return { ds, refetchEmpty };
});

vi.mock("./data", () => ({
  chatHost: () => ({
    engine: { request: async () => ({}) },
    runCommand: async () => {},
    openFile: async () => {},
    subscribe: () => () => {},
    workspacePath: () => null,
    account: () => ({ email: null, webAppUrl: null }),
    onAccountChange: () => () => {},
    auth: {
      openBrowser: async () => {},
      getState: () => undefined,
      setState: () => {},
      on: () => () => {},
      ready: () => {},
    },
  }),

  chatData: () => ds,
  refetchWhileErrored: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
  refetchWhileErroredOrEmpty: refetchEmpty,
  chatCaps: () => ({ opencodeActive: true }),
  isSessionNotOpen: () => false,
  errorText: (err: unknown) =>
    typeof err === "object" && err !== null && typeof (err as { message?: unknown }).message === "string"
      ? (err as { message: string }).message
      : String(err),
  exportBlob: async () => {},
}));

import { ChatSurface } from "./ChatSurface";
import { useChatStore } from "./chatStore";

/** The one catalog entry these tests hand the gateway; every expectation reads
 *  its name off this fixture, so an assertion cannot drift from what the surface
 *  was actually given. */
const CATALOG_MODEL = { id: "model-alpha", displayName: "Model Alpha", efforts: [], defaultEffort: null };
const CHIP_NAME = `Model: ${CATALOG_MODEL.displayName}`;

/** The reasons the fake gateway fails with. The banner has to carry the reason
 *  through verbatim, so the expectations are built from these. */
const FIRST_FAILURE = { message: "gateway is not answering" };
const SECOND_FAILURE = { message: "gateway stopped again" };
const reason = (failure: { message: string }): RegExp => new RegExp(failure.message, "i");

/** The model chip. Its accessible name carries the picked model, so a missing
 *  chip and a wrong chip are distinguishable. */
const modelChip = (): HTMLElement | null => screen.queryByRole("button", { name: /^model:/i });

function renderNewChat() {
  const qc = createQueryClient({ retry: false });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat" element={<ChatSurface />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, qc };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  ds.listModels.mockReset();
  ds.listModels.mockResolvedValue([] as unknown[]);
  refetchEmpty.mockReset();
  refetchEmpty.mockReturnValue(false);
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

describe("ChatSurface model catalog", () => {
  it("renders the gateway catalog and raises no banner", async () => {
    ds.listModels.mockResolvedValue([CATALOG_MODEL]);
    renderNewChat();

    await waitFor(() => expect(modelChip()).toHaveAccessibleName(CHIP_NAME));
    expect(screen.queryByText(/models unavailable/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^dismiss$/i })).not.toBeInTheDocument();
  });

  it("reports an empty catalog and offers no model to pick", async () => {
    ds.listModels.mockResolvedValue([]);
    renderNewChat();

    expect(await screen.findByText(/no models available from the model gateway/i)).toBeInTheDocument();
    // Nothing invented: with no catalog there is no chip claiming a model.
    expect(modelChip()).toBeNull();
  });

  it("reports the gateway's own reason when the catalog fetch fails", async () => {
    ds.listModels.mockRejectedValue(FIRST_FAILURE);
    renderNewChat();

    const banner = await screen.findByText(reason(FIRST_FAILURE));
    expect(banner).toHaveTextContent(/couldn't load models/i);
  });

  // Both catalog banners carry a dismiss control with an accessible name, so a
  // reader using assistive tech can target it.
  it.each([
    ["a gateway that refuses", () => ds.listModels.mockRejectedValue(FIRST_FAILURE), /couldn't load models/i],
    ["a gateway that answers with nothing", () => ds.listModels.mockResolvedValue([]), /no models available/i],
  ])("dismisses the banner raised by %s", async (_why, arrange, says) => {
    arrange();
    renderNewChat();

    expect(await screen.findByText(says)).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^dismiss$/i }));
    });
    expect(screen.queryByText(says)).not.toBeInTheDocument();
    expect(screen.queryByText(/models unavailable/i)).not.toBeInTheDocument();
  });

  it("raises the banner again on a new failure after a dismiss", async () => {
    // Dismissing must not silence FUTURE problems: the dismiss clears the moment
    // the problem itself clears, so a later distinct failure is heard.
    refetchEmpty.mockImplementation((q) => (q?.state?.status === "error" ? 20 : false));
    ds.listModels.mockReset();
    ds.listModels.mockRejectedValueOnce(FIRST_FAILURE);
    ds.listModels.mockResolvedValueOnce([CATALOG_MODEL]);
    ds.listModels.mockRejectedValue(SECOND_FAILURE);
    const { qc } = renderNewChat();

    expect(await screen.findByText(reason(FIRST_FAILURE))).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^dismiss$/i }));
    });
    // The error poll recovers to a real catalog: the chip shows it, which is how
    // the problem clearing (and the dismiss resetting with it) is observable.
    await waitFor(() => expect(modelChip()).toHaveAccessibleName(CHIP_NAME));
    expect(screen.queryByText(/models unavailable/i)).not.toBeInTheDocument();

    await act(async () => {
      await qc.refetchQueries();
    });
    expect(await screen.findByText(reason(SECOND_FAILURE))).toBeInTheDocument();
  });

  it("heals a transiently empty catalog by polling, with no reload", async () => {
    // A gateway 401 makes listModels return [] — a "success" the surface has to
    // poll its way out of.
    refetchEmpty.mockImplementation((q) => {
      const data = q?.state?.data;
      return Array.isArray(data) && data.length === 0 ? 20 : false;
    });
    ds.listModels.mockReset();
    ds.listModels.mockResolvedValueOnce([]);
    ds.listModels.mockResolvedValue([CATALOG_MODEL]);
    renderNewChat();

    expect(await screen.findByText(/no models available/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/no models available/i)).not.toBeInTheDocument());
    expect(modelChip()).toHaveAccessibleName(CHIP_NAME);
  });
});
