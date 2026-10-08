import "@/tests/fixtures/cloudProviders";
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MachineCard, WorkspaceMachineMoveRead, WorkspaceMachineRead } from "@/api/machines";
import {
  CHANGE_MACHINE,
  GRACE,
  MANAGE_MACHINES,
  MOVE_COPY,
  MOVE_FAILED,
  MOVE_WORKSPACE,
  STOP_RUNNING,
  WorkspaceMachineChip,
  forgetMove,
  moveFailure,
  moveOutcome,
  moveStatus,
} from "@/pages/workspace/workspaces/WorkspaceMachine";

import {
  json,
  machinesServer,
  orgMachine,
  refuse,
  removeTopbar,
  renderAt,
  type MachinesServer,
} from "../../organization/machines/machinesServer";

// The workspace header's machine chip and the change-machine dialog: what a
// mover is offered, the request a move sends, the steps a move reads as, and
// what a failed move offers.

const WS = "ws-1";
const LAB = orgMachine().card as MachineCard;
const SHARED: MachineCard = { kind: "shared", org_machine_id: null, name: "Shared machines", spec: null, state: "shared", step: null, step_started_at: null, step_expected_seconds: null, stop_reason: "", disk_full: false };
const H100: MachineCard = {
  ...LAB,
  org_machine_id: "m-h100",
  name: "H100 Box",
  spec: { ...LAB.spec!, gpu: { name: "H100", count: 2, memory_gb: 80 } },
  state: "stopped",
};

function read(over: Partial<WorkspaceMachineRead> = {}): WorkspaceMachineRead {
  return { card: SHARED, pin: null, active_move: null, can_move: true, targets: [SHARED, LAB, H100], ...over };
}

function moveRow(over: Partial<WorkspaceMachineMoveRead> = {}): WorkspaceMachineMoveRead {
  return {
    id: "mv-1",
    workspace_id: WS,
    from_org_machine_id: null,
    to_org_machine_id: "m-h100",
    state: "requested",
    error_code: "",
    error: "",
    requested_at: "2026-10-05T10:00:00Z",
    finished_at: null,
    ...over,
  };
}

let server: MachinesServer;
beforeEach(() => {
  server = machinesServer();
  server.workspaceMachines[WS] = read();
});
afterEach(() => {
  cleanup();
  removeTopbar();
  forgetMove(WS);
  vi.unstubAllGlobals();
});

const renderChip = () => renderAt(<WorkspaceMachineChip workspaceId={WS} workspaceVersion={7} />, "/workspaces/ws-1");

describe("the chip", () => {
  it("names where the workspace runs, and is read-only for someone who may not move it", async () => {
    server.workspaceMachines[WS] = read({ card: LAB, pin: LAB.org_machine_id, can_move: false, targets: [] });
    renderChip();
    await userEvent.click(await screen.findByRole("button", { name: "A100 Lab, Running" }));
    const dialog = await screen.findByRole("dialog", { name: "Machine" });
    expect(within(dialog).getByText("1x A100 80 GB · 16 vCPU · 125 GB memory · 200 GB disk · RunPod US-TX")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: MOVE_WORKSPACE })).toBeNull();
  });

  it("draws nothing for a workspace whose machine the server does not answer for", async () => {
    delete server.workspaceMachines[WS];
    renderChip();
    await waitFor(() => expect(server.sent.some((s) => s.path === `/api/v1/workspaces/${WS}/machine`)).toBe(true));
    expect(screen.queryByRole("button", { name: /Shared machines/ })).toBeNull();
  });
});

describe("change machine", () => {
  it("leads a team admin to the Machines page to buy or manage machines", async () => {
    renderChip();
    await userEvent.click(await screen.findByRole("button", { name: "Shared machines" }));
    const dialog = await screen.findByRole("dialog", { name: CHANGE_MACHINE });
    const link = await within(dialog).findByRole("link", { name: MANAGE_MACHINES });
    expect(link).toHaveAttribute("href", "/settings/organization/machines");
  });

  it("offers no Machines link to someone who cannot open that page", async () => {
    server.viewer = { ...server.viewer, isOrgAdmin: false, adminTeamIds: [] };
    renderChip();
    await userEvent.click(await screen.findByRole("button", { name: "Shared machines" }));
    const dialog = await screen.findByRole("dialog", { name: CHANGE_MACHINE });
    await waitFor(() => expect(server.sent.some((s) => s.path === "/api/v1/auth/me")).toBe(true));
    expect(within(dialog).queryByRole("link", { name: MANAGE_MACHINES })).toBeNull();
  });

  it("lists the targets, the current one marked, and moves to the one picked", async () => {
    renderChip();
    await userEvent.click(await screen.findByRole("button", { name: "Shared machines" }));
    const dialog = await screen.findByRole("dialog", { name: CHANGE_MACHINE });
    const radios = within(dialog).getAllByRole("radio");
    expect(radios.map((r) => r.getAttribute("aria-label"))).toEqual([
      "Shared machines, current machine",
      "1x A100 80 GB · 16 vCPU · 125 GB memory · 200 GB disk · RunPod US-TX · Running",
      "2x H100 80 GB · 16 vCPU · 125 GB memory · 200 GB disk · RunPod US-TX · Stopped",
    ]);
    expect(radios[0]).toHaveAttribute("aria-disabled", "true");
    expect(within(dialog).getByText(MOVE_COPY)).toBeInTheDocument();
    expect(within(dialog).getByRole("checkbox", { name: STOP_RUNNING })).toBeChecked();
    expect(within(dialog).queryByText(GRACE)).toBeNull();
    const move = within(dialog).getByRole("button", { name: MOVE_WORKSPACE });
    expect(move).toBeDisabled();

    server.writes[`POST /api/v1/workspaces/${WS}/machine`] = () => json(moveRow(), 202);
    await userEvent.click(radios[2]);
    await userEvent.click(move);
    await waitFor(() => expect(server.calls("POST", `/api/v1/workspaces/${WS}/machine`)).toHaveLength(1));
    const [sent] = server.calls("POST", `/api/v1/workspaces/${WS}/machine`);
    expect(sent.body).toEqual({ to_org_machine_id: "m-h100", stop_running: true });
    expect(sent.ifMatch).toBe("7");
  });

  it("lets running chats finish when the box is unticked", async () => {
    server.writes[`POST /api/v1/workspaces/${WS}/machine`] = () => json(moveRow(), 202);
    renderChip();
    await userEvent.click(await screen.findByRole("button", { name: "Shared machines" }));
    const dialog = await screen.findByRole("dialog", { name: CHANGE_MACHINE });
    await userEvent.click(within(dialog).getByRole("checkbox", { name: STOP_RUNNING }));
    expect(within(dialog).getByText(GRACE)).toBeInTheDocument();
    await userEvent.click(within(dialog).getAllByRole("radio")[1]);
    await userEvent.click(within(dialog).getByRole("button", { name: MOVE_WORKSPACE }));
    await waitFor(() => expect(server.calls("POST", `/api/v1/workspaces/${WS}/machine`)).toHaveLength(1));
    expect(server.calls("POST", `/api/v1/workspaces/${WS}/machine`)[0].body).toEqual({
      to_org_machine_id: LAB.org_machine_id,
      stop_running: false,
    });
  });

  it("says who would lose the machine when the target is not shared with them", async () => {
    server.writes[`POST /api/v1/workspaces/${WS}/machine`] = () =>
      refuse(409, "move_target_not_shared", "Ada has a running chat here and can't use H100 Box.");
    renderChip();
    await userEvent.click(await screen.findByRole("button", { name: "Shared machines" }));
    const dialog = await screen.findByRole("dialog", { name: CHANGE_MACHINE });
    await userEvent.click(within(dialog).getAllByRole("radio")[2]);
    await userEvent.click(within(dialog).getByRole("button", { name: MOVE_WORKSPACE }));
    expect(await within(dialog).findByText("Ada has a running chat here and can't use H100 Box.")).toBeInTheDocument();
  });

  it("follows the move's steps on the chip as the server reports them", async () => {
    let state: WorkspaceMachineMoveRead["state"] = "draining";
    server.writes[`GET /api/v1/workspaces/${WS}/machine`] = () => json(read({ pin: "m-h100", active_move: moveRow({ state }) }));
    const view = renderChip();
    expect(await screen.findByRole("button", { name: "Shared machines, Saving chats" })).toBeInTheDocument();
    state = "switching";
    await act(() => view.qc.invalidateQueries());
    expect(await screen.findByRole("button", { name: "Shared machines, Starting H100 Box" })).toBeInTheDocument();
    state = "waking";
    await act(() => view.qc.invalidateQueries());
    expect(await screen.findByRole("button", { name: "Shared machines, Waking workspace" })).toBeInTheDocument();
  });

  it("offers to cancel a move until chats have switched", async () => {
    server.workspaceMachines[WS] = read({ pin: "m-h100", active_move: moveRow({ state: "draining" }) });
    server.writes[`POST /api/v1/workspaces/${WS}/machine/moves/mv-1/cancel`] = () => json(undefined, 202);
    renderChip();
    await userEvent.click(await screen.findByRole("button", { name: /Saving chats/ }));
    const dialog = await screen.findByRole("dialog", { name: CHANGE_MACHINE });
    expect(within(dialog).getByRole("button", { name: MOVE_WORKSPACE })).toBeDisabled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel move" }));
    await waitFor(() => expect(server.calls("POST", `/api/v1/workspaces/${WS}/machine/moves/mv-1/cancel`)).toHaveLength(1));
  });

  it("reads a move it asked for that ended off its target as failed, and moves again on Retry", async () => {
    let answer = read();
    server.writes[`GET /api/v1/workspaces/${WS}/machine`] = () => json(answer);
    server.writes[`POST /api/v1/workspaces/${WS}/machine`] = () => {
      answer = read({ pin: "m-h100", active_move: moveRow() });
      return json(moveRow(), 202);
    };
    const view = renderChip();
    await userEvent.click(await screen.findByRole("button", { name: "Shared machines" }));
    let dialog = await screen.findByRole("dialog", { name: CHANGE_MACHINE });
    await userEvent.click(within(dialog).getAllByRole("radio")[2]);
    await userEvent.click(within(dialog).getByRole("button", { name: MOVE_WORKSPACE }));
    expect(await screen.findByRole("button", { name: "Shared machines, Saving chats" })).toBeInTheDocument();

    // The move ends and the server puts the pin back: the workspace never reached H100 Box.
    answer = read();
    await act(() => view.qc.invalidateQueries());
    const chip = await screen.findByRole("button", { name: `Shared machines, ${MOVE_FAILED}` });
    await userEvent.click(chip);
    dialog = await screen.findByRole("dialog", { name: CHANGE_MACHINE });
    expect(within(dialog).getByText("The workspace didn't move to H100 Box.")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(server.calls("POST", `/api/v1/workspaces/${WS}/machine`)).toHaveLength(2));
    expect(server.calls("POST", `/api/v1/workspaces/${WS}/machine`)[1].body).toMatchObject({ to_org_machine_id: "m-h100" });
  });

  it("forgets a move that landed", async () => {
    let answer = read();
    server.writes[`GET /api/v1/workspaces/${WS}/machine`] = () => json(answer);
    server.writes[`POST /api/v1/workspaces/${WS}/machine`] = () => {
      answer = read({ pin: "m-h100", active_move: moveRow() });
      return json(moveRow(), 202);
    };
    const view = renderChip();
    await userEvent.click(await screen.findByRole("button", { name: "Shared machines" }));
    const dialog = await screen.findByRole("dialog", { name: CHANGE_MACHINE });
    await userEvent.click(within(dialog).getAllByRole("radio")[2]);
    await userEvent.click(within(dialog).getByRole("button", { name: MOVE_WORKSPACE }));
    await screen.findByRole("button", { name: /Saving chats/ });
    answer = read({ card: H100, pin: "m-h100" });
    await act(() => view.qc.invalidateQueries());
    expect(await screen.findByRole("button", { name: "H100 Box, Stopped" })).toBeInTheDocument();
  });

  it("gives a manager of a target without hardware the way to new hardware", async () => {
    const failed = moveRow({ state: "failed", error_code: "target_capacity" });
    server.workspaceMachines[WS] = read({ active_move: failed });
    server.machines = [orgMachine({ id: "m-h100", name: "H100 Box" })];
    renderChip();
    await userEvent.click(await screen.findByRole("button", { name: `Shared machines, ${MOVE_FAILED}` }));
    const dialog = await screen.findByRole("dialog", { name: CHANGE_MACHINE });
    expect(within(dialog).getByText("No hardware is free for H100 Box right now.")).toBeInTheDocument();
    await userEvent.click(await within(dialog).findByRole("button", { name: "Start on new hardware" }));
    await waitFor(() => expect(server.calls("POST", "/api/v1/org/machines/m-h100/replace")).toHaveLength(1));
  });

  it("gives anyone else the choice of another machine instead", async () => {
    server.workspaceMachines[WS] = read({ active_move: moveRow({ state: "failed", error_code: "target_capacity" }) });
    server.machines = [orgMachine({ id: "m-h100", name: "H100 Box", can_manage: false })];
    renderChip();
    await userEvent.click(await screen.findByRole("button", { name: `Shared machines, ${MOVE_FAILED}` }));
    const dialog = await screen.findByRole("dialog", { name: CHANGE_MACHINE });
    expect(await within(dialog).findByRole("button", { name: "Choose another machine" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Start on new hardware" })).toBeNull();
  });
});

describe("the move's readings", () => {
  const targets = [SHARED, LAB, H100];

  it.each([
    ["requested", "Saving chats"],
    ["draining", "Saving chats"],
    ["switching", "Starting H100 Box"],
    ["waking", "Waking workspace"],
    ["failed", MOVE_FAILED],
    ["done", null],
  ] as const)("a move %s reads %s", (state, words) => {
    expect(moveStatus({ active_move: moveRow({ state }), targets })).toBe(words);
  });

  it.each([
    ["target_capacity", "No hardware is free for H100 Box right now.", true],
    ["target_boot_failed", "H100 Box couldn't start.", false],
    ["drain_timeout", "Running chats didn't stop in time.", false],
    ["not_allowed", "You can't use H100 Box.", false],
    ["", "The workspace didn't move to H100 Box.", false],
  ])("a failure %s reads %s", (code, message, capacity) => {
    expect(moveFailure(moveRow({ error_code: code }), targets)).toEqual({ message, capacity });
  });

  it("calls a move to the default done when the workspace has no pin", () => {
    expect(moveOutcome(read({ pin: null }), moveRow({ to_org_machine_id: null }))).toEqual({ kind: "done" });
    expect(moveOutcome(read({ pin: "m-h100" }), moveRow({ to_org_machine_id: null }))?.kind).toBe("failed");
    expect(moveOutcome(read(), null)).toBeNull();
  });

  it("reads how the asked move ended from the last move, with its own error", () => {
    const mine = moveRow({ to_org_machine_id: "m-h100" });
    // The pin went back, but the server says why: no hardware, not a guess.
    const failed = moveOutcome(
      read({ pin: null, last_move: moveRow({ state: "failed", error_code: "target_capacity" }) }),
      mine,
    );
    expect(failed).toEqual({
      kind: "failed",
      move: expect.objectContaining({ error_code: "target_capacity" }),
      failure: { message: "No hardware is free for H100 Box right now.", capacity: true },
    });
    // Ended done, or canceled by the reader: nothing to say, whatever the pin reads.
    expect(moveOutcome(read({ pin: null, last_move: moveRow({ state: "done" }) }), mine)).toEqual({ kind: "done" });
    expect(moveOutcome(read({ pin: null, last_move: moveRow({ state: "canceled" }) }), mine)).toEqual({ kind: "done" });
    // A move someone else asked for since is not this reader's failure.
    expect(
      moveOutcome(read({ pin: null, last_move: moveRow({ id: "mv-2", state: "failed" }) }), mine),
    ).toEqual({ kind: "done" });
  });
});
