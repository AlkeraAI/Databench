// Opening a workspace whose machine is gone: the server holds the wake and
// lists where the reader may wake it. The prompt draws that answer, with the
// default placement preselected when the server offers it, and a pick moves the
// workspace there and then asks for the wake again.

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useWakeWorkspaceOnOpen } from "@/api/chats";
import type { MachineCard, MachineUnavailableRead } from "@/api/machines";
import { createQueryClient } from "@/api/queryClient";
import {
  PICK_MACHINE,
  WAKE_ON,
  WakeMachinePrompt,
  forgetMove,
  lostSentence,
  lostStatus,
} from "@/pages/workspace/workspaces/WorkspaceMachine";

const WS = "33333333-3333-4333-8333-333333333333";

const STANDARD: MachineCard = {
  kind: "shared",
  org_machine_id: null,
  name: "Standard",
  spec: null,
  state: "shared",
  step: null,
  step_started_at: null,
  step_expected_seconds: null,
  stop_reason: "",
  disk_full: false,
};
const SPARE: MachineCard = { ...STANDARD, kind: "org_machine", org_machine_id: "m-spare", name: "Spare box", state: "running" };

function held(over: Partial<MachineUnavailableRead> = {}): MachineUnavailableRead {
  return {
    workspace_id: WS,
    lost: { org_machine_id: "m-old", name: "Old GPU", reason: "deleted", fell_back: false },
    choices: [STANDARD, SPARE],
    preselect_default: true,
    ...over,
  };
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

/** The server behind `fetch`: the first wake is held with `verdict`, every
 *  later one wakes. Returns every request, in order. */
function serve(verdict: MachineUnavailableRead): Sent[] {
  const sent: Sent[] = [];
  let wakes = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const path = new URL(request ? request.url : String(input), "http://x").pathname;
      const method = (request?.method ?? init?.method ?? "GET").toUpperCase();
      const text = request ? await request.clone().text() : typeof init?.body === "string" ? init.body : "";
      sent.push({ method, path, body: text ? JSON.parse(text) : undefined });
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
      if (method === "POST" && path === `/api/v1/workspaces/${WS}/wake`) {
        wakes += 1;
        return wakes === 1
          ? json({ outcome: "machine_unavailable", machine_unavailable: verdict })
          : json({ outcome: "waking", machine_unavailable: null });
      }
      if (method === "POST" && path === `/api/v1/workspaces/${WS}/machine`) {
        return json(
          {
            id: "mv-1",
            workspace_id: WS,
            from_org_machine_id: null,
            to_org_machine_id: (JSON.parse(text) as { to_org_machine_id: string | null }).to_org_machine_id,
            state: "requested",
            error_code: "",
            error: "",
            requested_at: "2026-10-06T10:00:00Z",
            finished_at: null,
          },
          202,
        );
      }
      return json({});
    }),
  );
  return sent;
}

function Host() {
  const opened = useWakeWorkspaceOnOpen(WS, false);
  return <WakeMachinePrompt held={opened.unavailable} onWake={opened.wake} />;
}

function renderHost() {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <Host />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const asked = (sent: Sent[]) =>
  sent.filter((s) => s.method === "POST" && s.path.startsWith(`/api/v1/workspaces/${WS}/`)).map((s) => s.path.split("/").pop());

afterEach(() => {
  cleanup();
  forgetMove(WS);
  vi.unstubAllGlobals();
});

describe("the wake prompt", () => {
  it("draws the held wake with Standard preselected, and wakes there on confirm", async () => {
    const sent = serve(held());
    renderHost();
    const dialog = await screen.findByRole("dialog", { name: PICK_MACHINE });
    expect(within(dialog).getByText("Old GPU was deleted. Wake on Standard, or pick a machine.")).toBeInTheDocument();
    const radios = within(dialog).getAllByRole("radio");
    expect(radios.map((r) => r.getAttribute("aria-checked"))).toEqual(["true", "false"]);

    await userEvent.click(within(dialog).getByRole("button", { name: WAKE_ON }));

    await waitFor(() => expect(asked(sent)).toEqual(["wake", "machine", "wake"]));
    const move = sent.find((s) => s.path === `/api/v1/workspaces/${WS}/machine`);
    expect(move?.body).toEqual({ to_org_machine_id: null, stop_running: false });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: PICK_MACHINE })).toBeNull());
  });

  it("moves to the machine picked, then asks for the wake again", async () => {
    const sent = serve(held());
    renderHost();
    const dialog = await screen.findByRole("dialog", { name: PICK_MACHINE });
    await userEvent.click(within(dialog).getAllByRole("radio")[1]);
    await userEvent.click(within(dialog).getByRole("button", { name: WAKE_ON }));

    await waitFor(() => expect(asked(sent)).toEqual(["wake", "machine", "wake"]));
    const move = sent.find((s) => s.path === `/api/v1/workspaces/${WS}/machine`);
    expect(move?.body).toEqual({ to_org_machine_id: "m-spare", stop_running: false });
  });

  it("preselects nothing and wakes nothing until a pick when the server offers no default", async () => {
    const sent = serve(held({ choices: [SPARE], preselect_default: false }));
    renderHost();
    const dialog = await screen.findByRole("dialog", { name: PICK_MACHINE });
    expect(within(dialog).getByText("Old GPU was deleted. Pick a machine to wake it on.")).toBeInTheDocument();
    expect(within(dialog).getByRole("radio")).toHaveAttribute("aria-checked", "false");
    expect(within(dialog).getByRole("button", { name: WAKE_ON })).toBeDisabled();
    expect(asked(sent)).toEqual(["wake"]);
  });

  it("tells a reader who may not move the workspace who can", async () => {
    serve(held({ choices: [], preselect_default: false }));
    renderHost();
    const dialog = await screen.findByRole("dialog", { name: PICK_MACHINE });
    expect(
      within(dialog).getByText("Old GPU was deleted. Ask someone with full access to this workspace to pick a machine."),
    ).toBeInTheDocument();
    expect(within(dialog).queryByRole("radio")).toBeNull();
    expect(within(dialog).queryByRole("button", { name: WAKE_ON })).toBeNull();
  });
});

describe("the lost machine's words", () => {
  it("names why the machine is gone", () => {
    expect(lostSentence({ name: "Old GPU", reason: "deleted" })).toBe("Old GPU was deleted.");
    expect(lostSentence({ name: "Old GPU", reason: "no_access" })).toBe("You can no longer use Old GPU.");
  });

  it("puts on the chip why the workspace waits, or where it went", () => {
    const lost = { org_machine_id: "m-old", name: "Old GPU", reason: "deleted" as const, fell_back: false };
    expect(lostStatus({ card: STANDARD, lost })).toBe("Machine deleted");
    expect(lostStatus({ card: STANDARD, lost: { ...lost, reason: "no_access" } })).toBe("Machine unavailable");
    expect(lostStatus({ card: STANDARD, lost: { ...lost, fell_back: true } })).toBe("Moved to Standard");
    expect(lostStatus({ card: STANDARD, lost: null })).toBeUndefined();
  });
});
