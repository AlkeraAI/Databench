import "@/tests/fixtures/cloudProviders";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OrgMachineRead } from "@/api/machines";
import { GROW_COPY, growTitle } from "@/pages/organization/machines/GrowDiskDialog";
import { MachineDetailPage, WAIT_COPY, diskFullLine } from "@/pages/organization/machines/MachineDetailPage";
import { waitLine } from "@alkera/ui";
import { replaceConsequence } from "@/pages/organization/machines/MachineActions";
import { COPY } from "@/pages/organization/machines/model";

import { machinesServer, orgMachine, removeTopbar, renderAt, type MachinesServer } from "./machinesServer";


// One machine's page: the inline editors that step aside when another admin got
// there first, the actions a manager gets for each state, and what a reader who
// only uses the machine is shown.

const ID = "00000000-0000-4000-8000-0000000000m1";
const PATH = `/settings/organization/machines/${ID}`;

let server: MachinesServer;
beforeEach(() => {
  server = machinesServer();
});
afterEach(() => {
  cleanup();
  removeTopbar();
  vi.unstubAllGlobals();
});

const renderPage = () => renderAt(<MachineDetailPage />, PATH, "/settings/organization/machines/:machineId");

function card(title: string): HTMLElement {
  return screen.getByRole("heading", { name: title }).closest(".alk-card") as HTMLElement;
}

describe("who can use it", () => {
  it("saves the edited audience with the version it read", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Who can use it" });
    await userEvent.click(within(card("Who can use it")).getByRole("button", { name: "Edit" }));
    await userEvent.click(within(card("Who can use it")).getByRole("button", { name: "Add everyone" }));
    await userEvent.click(within(card("Who can use it")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(server.calls("PUT", `/api/v1/org/machines/${ID}/audience`)).toHaveLength(1));
    const [put] = server.calls("PUT", `/api/v1/org/machines/${ID}/audience`);
    expect(put.ifMatch).toBe("3");
    expect(put.body).toEqual({
      audience: [
        { kind: "team", team_id: "00000000-0000-4000-8000-0000000000d1", user_id: null },
        { kind: "org", team_id: null, user_id: null },
      ],
    });
  });

  it("steps aside with the current list when another admin changed the machine first", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Who can use it" });
    await userEvent.click(within(card("Who can use it")).getByRole("button", { name: "Edit" }));
    // Another admin narrows it meanwhile.
    server.machines = [{ ...server.machines[0], version: 4, audience: [{ kind: "user", team_id: null, user_id: "u-ada", label: "Ada Lovelace" }] }];
    await userEvent.click(within(card("Who can use it")).getByRole("button", { name: "Remove Data" }));
    await userEvent.click(within(card("Who can use it")).getByRole("button", { name: "Save" }));
    expect(await within(card("Who can use it")).findByText(COPY.conflict)).toBeInTheDocument();
    // The editor closed on the server's current list, and the refused write changed nothing.
    expect(await within(card("Who can use it")).findByText("Ada Lovelace")).toBeInTheDocument();
    expect(within(card("Who can use it")).queryByRole("button", { name: "Save" })).toBeNull();
    expect(server.machines[0].audience.map((a) => a.label)).toEqual(["Ada Lovelace"]);
  });

  it("says a pool machine serves everyone, with nothing to edit", async () => {
    server.machines = [orgMachine({ use_mode: "pool", audience: [] })];
    renderPage();
    expect(await within(await screen.findByRole("heading", { name: "Who can use it" }).then((h) => h.closest(".alk-card") as HTMLElement)).findByText(COPY.everyone)).toBeInTheDocument();
    expect(within(card("Who can use it")).queryByRole("button", { name: "Edit" })).toBeNull();
  });
});

describe("settings", () => {
  it("edits in place and sends only what changed", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Settings" });
    expect(within(card("Settings")).getByText("30 minutes")).toBeInTheDocument();
    await userEvent.click(within(card("Settings")).getByRole("button", { name: "Edit" }));
    await userEvent.click(within(card("Settings")).getByLabelText("Stop when idle"));
    await userEvent.click(await screen.findByRole("option", { name: "Never" }));
    await userEvent.click(within(card("Settings")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(server.calls("PATCH", `/api/v1/org/machines/${ID}`)).toHaveLength(1));
    expect(server.calls("PATCH", `/api/v1/org/machines/${ID}`)[0].body).toEqual({ idle_stop_minutes: null });
    expect(await within(card("Settings")).findByText("Never")).toBeInTheDocument();
  });
});

describe("actions by state", () => {
  it.each([
    ["running", ["Stop"], ["Start", "Try again", "Start on new hardware"]],
    ["stopped", ["Start", "Start on new hardware"], ["Stop", "Try again"]],
    ["failed", ["Try again", "Start on new hardware"], ["Stop", "Start"]],
    ["waiting_for_hardware", ["Start on new hardware"], ["Start", "Try again"]],
  ])("a %s machine offers %j and not %j", async (state, offered, withheld) => {
    server.machines = [orgMachine({ card: { ...orgMachine().card, state: state as OrgMachineRead["card"]["state"] } })];
    renderPage();
    await screen.findByRole("heading", { name: "Settings" });
    for (const label of offered) expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    for (const label of withheld) expect(screen.queryByRole("button", { name: label })).toBeNull();
  });

  it("offers no new hardware for a machine the server says has none, whatever its state", async () => {
    server.machines = [
      orgMachine({ acquisition: "added", can_replace: false, card: { ...orgMachine().card, state: "stopped" } }),
    ];
    renderPage();
    await screen.findByRole("button", { name: "Start" });
    expect(screen.queryByRole("button", { name: "Start on new hardware" })).toBeNull();
  });

  it("asks before replacing, then asks for new hardware", async () => {
    server.machines = [orgMachine({ card: { ...orgMachine().card, state: "failed" } })];
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Start on new hardware" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(replaceConsequence("A100 Lab"))).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Start on new hardware" }));
    await waitFor(() => expect(server.calls("POST", `/api/v1/org/machines/${ID}/replace`)).toHaveLength(1));
    expect(server.calls("POST", `/api/v1/org/machines/${ID}/replace`)[0].ifMatch).toBe("3");
  });
});

describe("what a manager is told", () => {
  it("tells a manager nothing about funding, provider or spend with nothing installed, and a user no controls", async () => {
    server.machines = [orgMachine({ credit_state: "urgent", runway_minutes: 45 })];
    renderPage();
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.queryByText(/Credits/)).toBeNull();
    expect(screen.queryByText(/dedicated machine/)).toBeNull();
    expect(screen.queryByText("Paid with")).toBeNull();
    expect(screen.queryByText("This cycle")).toBeNull();
    expect(screen.queryByText("Monthly cap")).toBeNull();
    cleanup();
    removeTopbar();
    server.machines = [orgMachine({ can_manage: false, spend_this_cycle_nanos: null })];
    renderPage();
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  });

  it("says a machine whose worker cannot start runs no chat and is not billed, and lets a manager stop it", async () => {
    const base = orgMachine();
    const message = "You are not billed for its running time until chats can start.";
    server.machines = [orgMachine({ card: { ...base.card, state: "unhealthy", fault: { code: "cgroup_refused", message, summary: "" } } })];
    renderPage();
    expect(await screen.findByText(`A100 Lab can't run chats. ${message}`)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("shows the server's reason a machine that never registered couldn't start", async () => {
    const base = orgMachine();
    const sentence =
      'A100 Lab couldn\'t start because its node never registered with this server. Its last error was "ApiError: unreachable: Connection refused".';
    server.machines = [
      orgMachine({
        card: {
          ...base.card,
          state: "failed",
          status: {
            subject: "machine",
            state: "failed",
            label: "Couldn't start",
            tone: "danger",
            reason_code: "never_registered_error",
            sentence,
          },
        },
      }),
    ];
    renderPage();
    expect(await screen.findByText(sentence)).toBeInTheDocument();
  });

  it("adds no reason callout to a failed machine the server gave none", async () => {
    const base = orgMachine();
    server.machines = [
      orgMachine({
        card: {
          ...base.card,
          state: "failed",
          status: { subject: "machine", state: "failed", label: "Couldn't start", tone: "danger", reason_code: "", sentence: "A100 Lab couldn't start." },
        },
      }),
    ];
    renderPage();
    await screen.findByRole("heading", { name: "Who can use it" });
    expect(screen.queryByText("A100 Lab couldn't start.")).toBeNull();
  });

  it("says a machine whose disk the server calls full is almost out of disk, and offers to grow it", async () => {
    const base = orgMachine();
    server.machines = [
      orgMachine({ card: { ...base.card, disk_full: true }, disk_grow: { min_gb: 201, max_gb: 1000, restarts: true } }),
    ];
    renderPage();
    expect(await screen.findByText(diskFullLine("A100 Lab"))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: GROW_COPY.confirm }));
    expect(await screen.findByRole("dialog", { name: growTitle("A100 Lab") })).toBeInTheDocument();
  });

  it("offers no grow on a full disk that cannot grow, and says nothing of a disk that is not full", async () => {
    const base = orgMachine();
    server.machines = [orgMachine({ card: { ...base.card, disk_full: true }, disk_grow: null })];
    renderPage();
    expect(await screen.findByText(diskFullLine("A100 Lab"))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: GROW_COPY.confirm })).toBeNull();
    cleanup();
    server.machines = [orgMachine({ disk_grow: { min_gb: 201, max_gb: 1000, restarts: true } })];
    renderPage();
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.queryByText(diskFullLine("A100 Lab"))).toBeNull();
    expect(screen.queryByRole("button", { name: GROW_COPY.confirm })).toBeNull();
  });

  it("says why a machine waits for hardware and when it is tried again, and offers a way out", async () => {
    const base = orgMachine();
    const wait = {
      reason: "RunPod has no 8 vCPU hosts right now.",
      next_try_at: "2026-10-06T19:47:00Z",
      gives_up_at: "2026-10-06T20:12:00Z",
    };
    server.machines = [orgMachine({ card: { ...base.card, state: "waiting_for_hardware", wait } })];
    renderPage();
    const line = waitLine(wait);
    expect(line.startsWith("RunPod has no 8 vCPU hosts right now. Trying again at ")).toBe(true);
    // On the page, and on the machine's state under its name.
    await waitFor(() => expect(screen.getAllByText(line)).toHaveLength(2));
    await userEvent.click(screen.getByRole("button", { name: WAIT_COPY.cancel }));
    expect(await screen.findByRole("dialog", { name: "Delete A100 Lab?" })).toBeInTheDocument();
  });

  it("says nothing of a fault on a machine that serves", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.queryByText(/can't run chats/)).toBeNull();
  });

  it("lists the workspaces on the machine and its timeline", async () => {
    server.details[ID] = {
      workspaces: [{ id: "ws-1", name: "Fine-tuning" }],
      timeline: [{ at: "2026-10-05T10:00:00Z", words: "Started on new hardware." }],
    };
    renderPage();
    expect(await screen.findByRole("link", { name: "Fine-tuning" })).toHaveAttribute("href", "/workspaces/ws-1");
    expect(screen.getByText("Started on new hardware.")).toBeInTheDocument();
  });

  it("goes back to the machines list once a delete is accepted", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Actions for A100 Lab" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox"), "A100 Lab");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(await screen.findByTestId("location")).toHaveTextContent(/^\/settings\/organization\/machines$/);
    expect(server.calls("DELETE", `/api/v1/org/machines/${ID}`)).toHaveLength(1);
    expect(screen.queryByText("This machine doesn't exist")).toBeNull();
  });

  it("stays on the machine when the delete is refused", async () => {
    server.writes[`DELETE /api/v1/org/machines/${ID}`] = () => new Response(JSON.stringify({ detail: "no" }), { status: 409 });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Actions for A100 Lab" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox"), "A100 Lab");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(await screen.findByText(COPY.conflict)).toBeInTheDocument();
    expect(screen.queryByTestId("location")).toBeNull();
  });

  it("says a machine it cannot find does not exist", async () => {
    server.machines = [];
    renderPage();
    expect(await screen.findByText("This machine doesn't exist")).toBeInTheDocument();
  });
});

