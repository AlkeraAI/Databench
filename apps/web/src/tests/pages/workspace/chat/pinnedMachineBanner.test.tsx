import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MachineCard } from "@/api/machines";
import { MACHINE_COPY, pinnedMachineCopy } from "@/pages/workspace/chat/MachineBanner";
import { PinnedMachineBanner } from "@/pages/workspace/chat/PinnedMachineBanner";
import { forgetMove } from "@/pages/workspace/workspaces/WorkspaceMachine";

import { machinesServer, orgMachine, removeTopbar, renderAt, type MachinesServer } from "../../organization/machines/machinesServer";


// A chat in a workspace pinned to an org machine is told about that machine by
// name: each state's words, and the way on each reader is offered.

const NOW = Date.parse("2026-10-05T12:00:00Z");
const LAB = orgMachine().card as MachineCard;
const card = (over: Partial<MachineCard>): MachineCard => ({ ...LAB, ...over });
const asMember = { manager: false, canMove: true, now: NOW };
const asManager = { manager: true, mayAddCredits: true, canMove: true, now: NOW };

describe("pinnedMachineCopy", () => {
  it.each([
    [
      "starting, three minutes left in its step",
      card({ state: "starting", step: "installing", step_started_at: new Date(NOW - 60_000).toISOString(), step_expected_seconds: 240 }),
      asMember,
      { title: "Starting A100 Lab.", body: "About 3 minutes.", actions: [] },
    ],
    ["starting with no timing", card({ state: "starting", step: "reserving" }), asMember, { title: "Starting A100 Lab.", body: MACHINE_COPY.starting.body, actions: [] }],
    [
      "waiting for its GPU",
      card({ state: "waiting_for_hardware" }),
      asMember,
      { title: "No A100 is free right now.", body: "We keep trying.", actions: ["change_machine"] },
    ],
    [
      "waiting, for a reader who may not move the workspace",
      card({ state: "waiting_for_hardware" }),
      { ...asMember, canMove: false },
      { title: "No A100 is free right now.", body: "We keep trying.", actions: [] },
    ],
    [
      "stopped after it stopped responding",
      card({ state: "stopped", stop_reason: "not_responding" }),
      asMember,
      { title: "A100 Lab stopped because it wasn't responding.", body: "Sending a message starts it again.", actions: ["change_machine"] },
    ],
    ["failed, to a manager", card({ state: "failed" }), asManager, { title: "A100 Lab couldn't start.", actions: ["try_again", "change_machine"] }],
    ["failed, to a member", card({ state: "failed" }), asMember, { title: "A100 Lab couldn't start.", actions: ["change_machine"] }],
    ["not responding", card({ state: "unreachable" }), asMember, { title: "A100 Lab isn't responding.", body: "Reconnecting…", actions: [] }],
    [
      "unable to run chats, in the server's words",
      card({ state: "unhealthy", fault: { code: "cgroup_refused", message: "You are not billed for its running time until chats can start.", summary: "" } }),
      asMember,
      { title: "A100 Lab can't run chats.", body: "You are not billed for its running time until chats can start.", actions: ["change_machine"] },
    ],
  ])("%s", (_what, c, who, expected) => {
    expect(pinnedMachineCopy(c, who)).toMatchObject(expected);
  });

  it.each([
    ["failed", card({ state: "failed" }), true],
    ["waiting for hardware", card({ state: "waiting_for_hardware" }), true],
    ["stopped when idle", card({ state: "stopped", stop_reason: "idle" }), false],
    ["starting", card({ state: "starting" }), false],
    ["not responding", card({ state: "unreachable" }), false],
    ["that cannot run chats", card({ state: "unhealthy" }), true],
  ])("says whether a message wakes a machine %s", (_what, c, blocks) => {
    expect(pinnedMachineCopy(c, asMember)?.blocksWake === true).toBe(blocks);
  });

  it("draws a stopped machine as the quiet line: a message starts it", () => {
    expect(pinnedMachineCopy(card({ state: "stopped", stop_reason: "idle" }), asMember)).toMatchObject({
      title: "A100 Lab is stopped.",
      body: "Sending a message starts it.",
      quiet: true,
    });
  });

  it("reads a stop for money as an ordinary stop when nothing installed names it", () => {
    expect(pinnedMachineCopy(card({ state: "stopped", stop_reason: "credits" }), asManager)).toMatchObject({
      title: "A100 Lab is stopped.",
      actions: [],
    });
    expect(pinnedMachineCopy(card({ state: "stopping", stop_reason: "cap" }), asManager)).toBeNull();
  });

  it.each([
    ["a running machine", card({ state: "running" })],
    ["a machine stopping on purpose", card({ state: "stopping", stop_reason: "user" })],
    ["the shared machines", card({ kind: "shared", state: "shared", name: "Shared machines" })],
  ])("says nothing for %s", (_what, c) => {
    expect(pinnedMachineCopy(c, asMember)).toBeNull();
  });
});

describe("PinnedMachineBanner", () => {
  let server: MachinesServer;
  beforeEach(() => {
    server = machinesServer();
  });
  afterEach(() => {
    cleanup();
    removeTopbar();
    forgetMove("ws-1");
    vi.unstubAllGlobals();
  });

  const pin = (c: MachineCard) => {
    server.workspaceMachines["ws-1"] = { card: c, pin: c.org_machine_id ?? null, active_move: null, can_move: true, targets: [c] };
  };
  const renderBanner = () =>
    renderAt(<PinnedMachineBanner workspaceId="ws-1" fallback={<p>Chat banner</p>} />, "/chat/c1");

  it("lets a manager try a failed machine again", async () => {
    pin(card({ state: "failed" }));
    server.machines = [orgMachine({ card: card({ state: "failed" }) })];
    renderBanner();
    const banner = await screen.findByRole("status");
    await userEvent.click(await within(banner).findByRole("button", { name: "Try again" }));
    await waitFor(() => expect(server.calls("POST", `/api/v1/org/machines/${LAB.org_machine_id}/start`)).toHaveLength(1));
  });

  it("opens the change-machine dialog from the banner", async () => {
    pin(card({ state: "waiting_for_hardware" }));
    renderBanner();
    await userEvent.click(await screen.findByRole("button", { name: "Change machine" }));
    expect(await screen.findByRole("dialog", { name: "Change workspace machine" })).toBeInTheDocument();
  });

  it("leaves the chat's own banner when the pinned machine has nothing to say", async () => {
    pin(card({ state: "running" }));
    renderBanner();
    expect(await screen.findByText("Chat banner")).toBeInTheDocument();
  });
});
