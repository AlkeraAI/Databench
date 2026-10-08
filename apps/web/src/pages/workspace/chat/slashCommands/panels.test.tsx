import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ClearConfirmPanel, CostPanel, ModePanel, TitlePanel, UsagePanel } from "./panels";
import type { SlashContext } from "./types";

function stubCtx(over: Partial<SlashContext> = {}): SlashContext {
  return {
    chatId: "c1",
    currentMode: "default",
    currentTitle: null,
    runCommand: vi.fn(async () => ({ kind: "ok" as const, command: null, payload: {}, message: null })),
    getUsage: vi.fn(async () => ({ credits: {}, usage: {} })),
    setMode: vi.fn(async () => {}),
    getCostState: vi.fn(async () => ({ spent: {}, caps: {}, orgManaged: false, unknownKeys: [] })),
    openPreferences: vi.fn(),
    showOnboarding: vi.fn(),
    exit: vi.fn(),
    onTitleChanged: vi.fn(),
    ...over,
  };
}

const api = { close: vi.fn() };

function draw(node: React.ReactElement) {
  return render(node);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("UsagePanel", () => {
  it("fetches the default window and shows the request count, and no plan or credit rows", async () => {
    const ctx = stubCtx({
      getUsage: vi.fn(async () => ({
        credits: { tier_name: "Pro", pct_used: 36.5, prepaid_credits: 1200 },
        usage: { total_requests: 8 },
      })),
    });
    draw(<UsagePanel ctx={ctx} api={api} />);

    expect(await screen.findByText("Requests (30d)")).toBeInTheDocument();
    expect(screen.getByText("8")).toBeInTheDocument();
    // With no extension installed, nothing reads the payload's credits.
    expect(screen.queryByText("Plan")).not.toBeInTheDocument();
    expect(screen.queryByText("Pro")).not.toBeInTheDocument();
    expect(screen.queryByText(/credits/i)).not.toBeInTheDocument();
    expect(ctx.getUsage).toHaveBeenCalledWith("30d");
    // The window picker is a labeled group (assistive tech announces what it switches).
    expect(screen.getByRole("tablist", { name: /usage window/i })).toBeInTheDocument();
  });

  it("refetches on a window pick, by click or by arrow key", async () => {
    const ctx = stubCtx();
    draw(<UsagePanel ctx={ctx} api={api} />);
    await waitFor(() => expect(ctx.getUsage).toHaveBeenCalledWith("30d"));

    fireEvent.click(screen.getByRole("tab", { name: "90d" }));
    await waitFor(() => expect(ctx.getUsage).toHaveBeenCalledWith("90d"));

    // 90d (index 2) → back to 30d (index 1) on ArrowLeft, then forward again.
    const picker = screen.getByRole("tablist", { name: /usage window/i });
    fireEvent.keyDown(picker, { key: "ArrowLeft" });
    await waitFor(() => expect(ctx.getUsage).toHaveBeenLastCalledWith("30d"));
    fireEvent.keyDown(picker, { key: "ArrowRight" });
    await waitFor(() => expect(ctx.getUsage).toHaveBeenLastCalledWith("90d"));
  });

  it("shows a sign-in prompt when usage rejects with AUTH_REQUIRED", async () => {
    const ctx = stubCtx({ getUsage: vi.fn(async () => Promise.reject({ code: -32001 })) });
    draw(<UsagePanel ctx={ctx} api={api} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign in to view your usage.");
  });
});

describe("CostPanel", () => {
  it("renders the spend-vs-cap rows from the cost state", async () => {
    const ctx = stubCtx({
      getCostState: vi.fn(async () => ({
        spent: { chat: 1.5, day: 4, week: 9 },
        caps: { chat: 50, day: 200, week: 500, per_query: 5 },
        orgManaged: true,
        unknownKeys: [],
      })),
    });
    draw(<CostPanel ctx={ctx} api={api} />);

    expect(await screen.findByText("$1.50")).toBeInTheDocument();
    expect(screen.getByText("$50.00")).toBeInTheDocument();
    expect(screen.getByText("$5.00")).toBeInTheDocument();
    expect(screen.getByText(/org-managed/i)).toBeInTheDocument();
  });
});

describe("ModePanel", () => {
  it("applies a picked mode and closes", async () => {
    const ctx = stubCtx();
    draw(<ModePanel ctx={ctx} api={api} />);

    fireEvent.click(screen.getByText("Plan").closest("button") as HTMLButtonElement);
    await waitFor(() => expect(ctx.setMode).toHaveBeenCalledWith("plan"));
    expect(api.close).toHaveBeenCalled();
  });

  // The cursor starts on the current mode ("default", index 0). ArrowDown steps to
  // Plan; ArrowUp from the first entry wraps around to the last (bypass).
  it.each([
    ["ArrowDown", "plan"],
    ["ArrowUp", "bypass"],
  ])("applies the mode %s highlights on Enter", async (key, mode) => {
    const ctx = stubCtx();
    draw(<ModePanel ctx={ctx} api={api} />);
    const group = screen.getByRole("radiogroup", { name: /permission mode/i });

    fireEvent.keyDown(group, { key });
    fireEvent.keyDown(group, { key: "Enter" });
    await waitFor(() => expect(ctx.setMode).toHaveBeenCalledWith(mode));
    expect(api.close).toHaveBeenCalled();
  });

  it("stays open and re-arms when setMode fails", async () => {
    const ctx = stubCtx({ setMode: vi.fn(async () => { throw new Error("daemon down"); }) });
    draw(<ModePanel ctx={ctx} api={api} />);

    const planBtn = screen.getByText("Plan").closest("button") as HTMLButtonElement;
    fireEvent.click(planBtn);
    await waitFor(() => expect(ctx.setMode).toHaveBeenCalledWith("plan"));
    // The failure path must NOT consume the command — the panel stays for a retry.
    expect(api.close).not.toHaveBeenCalled();
    // ...and the buttons come back (busy cleared), so the user can pick again.
    await waitFor(() => expect(planBtn).not.toBeDisabled());
  });

  it("ignores a second pick while the first setMode is still in flight", async () => {
    // The buttons disable on busy; a double-click (or a frantic second choice)
    // must not fire a competing setMode that races the first.
    let release: () => void = () => {};
    const setMode = vi.fn(() => new Promise<void>((r) => { release = r; }));
    const ctx = stubCtx({ setMode });
    draw(<ModePanel ctx={ctx} api={api} />);

    fireEvent.click(screen.getByText("Plan").closest("button") as HTMLButtonElement);
    fireEvent.click(screen.getByText("Auto").closest("button") as HTMLButtonElement);
    expect(setMode).toHaveBeenCalledTimes(1);
    expect(setMode).toHaveBeenCalledWith("plan");
    release();
    await waitFor(() => expect(api.close).toHaveBeenCalled());
  });
});

describe("TitlePanel", () => {
  it("sets the title, notifies, and closes", async () => {
    const ctx = stubCtx({ currentTitle: "Old" });
    draw(<TitlePanel ctx={ctx} api={api} />);

    const field = screen.getByRole("textbox", { name: /chat title/i });
    fireEvent.change(field, { target: { value: "New name" } });
    fireEvent.click(screen.getByRole("button", { name: /save title/i }));

    await waitFor(() => expect(ctx.runCommand).toHaveBeenCalledWith("/title New name"));
    expect(ctx.onTitleChanged).toHaveBeenCalled();
    expect(api.close).toHaveBeenCalled();
  });

  it("refuses a blank title from both Save and Enter", () => {
    const ctx = stubCtx();
    draw(<TitlePanel ctx={ctx} api={api} />);
    const field = screen.getByRole("textbox", { name: /chat title/i });
    // A whitespace-only title is not a name — saving it would blank the chat.
    fireEvent.change(field, { target: { value: "   " } });
    expect(screen.getByRole("button", { name: /save title/i })).toBeDisabled();
    fireEvent.keyDown(field, { key: "Enter" });
    expect(ctx.runCommand).not.toHaveBeenCalled();
    expect(api.close).not.toHaveBeenCalled();
  });

  it("keeps the panel open and unsaved when the title write fails", async () => {
    const ctx = stubCtx({ runCommand: vi.fn(async () => { throw new Error("write failed"); }) });
    draw(<TitlePanel ctx={ctx} api={api} />);

    fireEvent.change(screen.getByRole("textbox", { name: /chat title/i }), { target: { value: "New name" } });
    fireEvent.click(screen.getByRole("button", { name: /save title/i }));
    await waitFor(() => expect(ctx.runCommand).toHaveBeenCalledWith("/title New name"));
    // A failed write must not pretend success: no chat-list refresh, no consume.
    expect(ctx.onTitleChanged).not.toHaveBeenCalled();
    expect(api.close).not.toHaveBeenCalled();
    // The Save button re-enables so the user can retry.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /save title/i })).not.toBeDisabled(),
    );
  });

  it("submits the title trimmed of surrounding whitespace", async () => {
    // The field value rides into the /title line; leading/trailing spaces would
    // persist a padded title, so submit trims before dispatch.
    const ctx = stubCtx();
    draw(<TitlePanel ctx={ctx} api={api} />);
    fireEvent.change(screen.getByRole("textbox", { name: /chat title/i }), { target: { value: "  Padded title  " } });
    fireEvent.click(screen.getByRole("button", { name: /save title/i }));
    await waitFor(() => expect(ctx.runCommand).toHaveBeenCalledWith("/title Padded title"));
  });
});

describe("ClearConfirmPanel", () => {
  it("clears only after confirmation", async () => {
    const ctx = stubCtx();
    draw(<ClearConfirmPanel ctx={ctx} api={api} />);

    fireEvent.click(screen.getByRole("button", { name: /clear conversation/i }));
    await waitFor(() => expect(ctx.runCommand).toHaveBeenCalledWith("/clear"));
    expect(api.close).toHaveBeenCalled();
  });

  it("cancel closes without clearing the conversation", () => {
    const ctx = stubCtx();
    draw(<ClearConfirmPanel ctx={ctx} api={api} />);

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(ctx.runCommand).not.toHaveBeenCalled();
    expect(api.close).toHaveBeenCalled();
  });

  it("keeps the confirm panel open when the clear fails", async () => {
    const ctx = stubCtx({ runCommand: vi.fn(async () => { throw new Error("clear failed"); }) });
    draw(<ClearConfirmPanel ctx={ctx} api={api} />);

    fireEvent.click(screen.getByRole("button", { name: /clear conversation/i }));
    await waitFor(() => expect(ctx.runCommand).toHaveBeenCalledWith("/clear"));
    // A failed clear must not close the panel as if it worked.
    expect(api.close).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /clear conversation/i })).not.toBeDisabled(),
    );
  });
});
