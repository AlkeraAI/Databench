import "@/tests/fixtures/cloudProviders";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MachinesPage } from "@/pages/organization/machines/MachinesPage";
import { COPY } from "@/pages/organization/machines/model";

import { TEAM_DATA, machinesServer, orgMachine, removeTopbar, renderAt, type MachinesServer } from "./machinesServer";


// The org's Machines page through its real hooks and the real mutation policy,
// with only the network stood in for: what each reader is shown, and that every
// row action sends its request with the version the reader saw.

let server: MachinesServer;
beforeEach(() => {
  server = machinesServer();
});
afterEach(() => {
  cleanup();
  removeTopbar();
  vi.unstubAllGlobals();
});

const PATH = "/settings/organization/machines";
const renderPage = () => renderAt(<MachinesPage />, PATH);

async function row(name: string): Promise<HTMLElement> {
  const cell = await screen.findByText(name, { selector: ".alk-machine__name" });
  return cell.closest("tr") as HTMLElement;
}

async function openMenu(name: string) {
  await userEvent.click(await screen.findByRole("button", { name: `Actions for ${name}` }));
}

describe("the list", () => {
  it("shows each machine's hardware, use and state, and no rate or spend", async () => {
    renderPage();
    const r = await row("A100 Lab");
    expect(within(r).getByText("1x A100 80 GB · 16 vCPU · 125 GB memory · 200 GB disk · RunPod US-TX")).toBeInTheDocument();
    expect(within(r).getByText("Assigned to")).toBeInTheDocument();
    expect(within(r).getByText("Running")).toBeInTheDocument();
    // Nothing installed prices machines, so the figures the server sends are not drawn.
    expect(within(r).queryByText("$1.89/hour")).toBeNull();
    expect(within(r).queryByText("$12.34")).toBeNull();
    expect(screen.queryByRole("columnheader", { name: "Rate" })).toBeNull();
    expect(screen.queryByRole("columnheader", { name: "This cycle" })).toBeNull();
  });

  it("carries the machine's whole name and hardware on its link, so a narrow column never hides the spec", async () => {
    renderPage();
    const r = await row("A100 Lab");
    const link = within(r).getByRole("link");
    expect(link).toHaveAttribute("title", "A100 Lab: 1x A100 80 GB · 16 vCPU · 125 GB memory · 200 GB disk · RunPod US-TX");
    // The full spec line is in the cell as text, not cut to fit.
    expect(within(link).getByText("1x A100 80 GB · 16 vCPU · 125 GB memory · 200 GB disk · RunPod US-TX")).toBeInTheDocument();
  });

  it.each([
    ["an org pool machine the server allows", { use_mode: "pool" as const, audience: [] }, true, "Org pool"],
    ["an org pool machine the server does not allow, with nothing installed to say why", { use_mode: "pool" as const, audience: [] }, false, "Org pool"],
    ["an assigned machine nobody may use", { audience: [] }, false, COPY.noAudience],
  ])("reads %s as its use", async (_what, over, enterprise, label) => {
    server.viewer = { ...server.viewer, enterprise };
    server.machines = [orgMachine(over)];
    renderPage();
    expect(within(await row("A100 Lab")).getByText(label)).toBeInTheDocument();
  });

  it("says when the org holds none, with the way to add one and nothing to buy", async () => {
    server.machines = [];
    server.viewer = { ...server.viewer, canAdd: true };
    renderPage();
    expect(await screen.findByText(COPY.empty)).toBeInTheDocument();
    expect((await screen.findAllByRole("button", { name: COPY.add })).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Buy machine" })).toBeNull();
    // The offerings are never asked for, and no plan note is drawn.
    expect(server.calls("GET", "/api/v1/machines/offerings")).toHaveLength(0);
    expect(screen.queryByRole("link", { name: "See plans" })).toBeNull();
  });
});

describe("row actions", () => {
  it("stops a running machine with the version it read, letting chats finish unless asked", async () => {
    renderPage();
    await openMenu("A100 Lab");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Running chats get up to 2 minutes to finish. Its disk is kept.")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(server.calls("POST", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1/stop")).toHaveLength(1));
    const [stop] = server.calls("POST", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1/stop");
    expect(stop.ifMatch).toBe("3");
    expect(stop.search).toBe("?now=false");
    // The list re-read the server: the machine now reads as stopping.
    expect(await within(await row("A100 Lab")).findByText("Stopping")).toBeInTheDocument();
  });

  it("stops running chats at once when asked to", async () => {
    renderPage();
    await openMenu("A100 Lab");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("checkbox", { name: "Stop running chats now" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(server.sent.some((s) => s.path.endsWith("/stop") && s.search === "?now=true")).toBe(true));
  });

  it("starts a stopped machine, and offers no Stop for it", async () => {
    server.machines = [orgMachine({ card: { ...orgMachine().card, state: "stopped" } })];
    renderPage();
    await openMenu("A100 Lab");
    expect(screen.queryByRole("menuitem", { name: "Stop" })).toBeNull();
    await userEvent.click(await screen.findByRole("menuitem", { name: "Start" }));
    await waitFor(() => expect(server.calls("POST", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1/start")).toHaveLength(1));
    expect(server.calls("POST", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1/start")[0].ifMatch).toBe("3");
  });

  it("renames with only the name and the version it read", async () => {
    renderPage();
    await openMenu("A100 Lab");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const dialog = await screen.findByRole("dialog");
    const field = within(dialog).getByLabelText("Name");
    await userEvent.clear(field);
    await userEvent.type(field, "Training box");
    await userEvent.click(within(dialog).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(server.calls("PATCH", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1")).toHaveLength(1));
    const [patch] = server.calls("PATCH", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1");
    expect(patch.body).toEqual({ name: "Training box" });
    expect(patch.ifMatch).toBe("3");
    expect(await screen.findByText("Training box", { selector: ".alk-machine__name" })).toBeInTheDocument();
  });

  it("keeps the rename open and says so when the name is taken", async () => {
    server.writes["PATCH /api/v1/org/machines/00000000-0000-4000-8000-0000000000m1"] = () =>
      new Response(JSON.stringify({ detail: { code: "name_taken", message: "Another machine already has this name." } }), {
        status: 409,
        headers: { "content-type": "application/json" },
      });
    renderPage();
    await openMenu("A100 Lab");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "2");
    await userEvent.click(within(dialog).getByRole("button", { name: "Rename" }));
    expect(await within(dialog).findByText("Another machine already has this name.")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("sends only the settings that changed, and offers no monthly cap", async () => {
    renderPage();
    await openMenu("A100 Lab");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Settings" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText("Monthly cap")).toBeNull();
    await userEvent.click(within(dialog).getByLabelText("Stop when idle"));
    await userEvent.click(await screen.findByRole("option", { name: "60 minutes" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(server.calls("PATCH", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1")).toHaveLength(1));
    expect(server.calls("PATCH", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1")[0].body).toEqual({
      idle_stop_minutes: 60,
    });
  });

  it("closes an edit another admin beat to it and says the current settings are shown", async () => {
    renderPage();
    await openMenu("A100 Lab");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Settings" }));
    // Another admin saves first: the server's machine moves on under the dialog.
    server.machines = [{ ...server.machines[0], version: 4, idle_stop_minutes: 240 }];
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByLabelText("Stop when idle"));
    await userEvent.click(await screen.findByRole("option", { name: "60 minutes" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await screen.findByText(COPY.conflict)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(server.machines[0].idle_stop_minutes).toBe(240);
  });

  it("deletes only once the machine's name is typed, with the version it read", async () => {
    renderPage();
    await openMenu("A100 Lab");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Deleting A100 Lab destroys its disk. Files in your workspaces are kept.")).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: "Delete" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByRole("textbox"), "A100 Lab");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    await waitFor(() => expect(server.calls("DELETE", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1")).toHaveLength(1));
    expect(server.calls("DELETE", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1")[0].ifMatch).toBe("3");
    expect(await screen.findByText(COPY.empty)).toBeInTheDocument();
  });

  it("replaces who may use the machine with the grants as picked", async () => {
    renderPage();
    await openMenu("A100 Lab");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Who can use it" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove Data" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Member" }));
    await userEvent.click(within(dialog).getByLabelText("Member"));
    await userEvent.click(await screen.findByRole("option", { name: "Ada Lovelace" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Add" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(server.calls("PUT", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1/audience")).toHaveLength(1),
    );
    const [put] = server.calls("PUT", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1/audience");
    expect(put.body).toEqual({ audience: [{ kind: "user", team_id: null, user_id: "u-ada" }] });
    expect(put.ifMatch).toBe("3");
  });
});

describe("a team admin's audience picker", () => {
  it("names only their teams and their teams' people, never the whole org", async () => {
    server.viewer = { isOrgAdmin: false, adminTeamIds: [TEAM_DATA] };
    renderPage();
    await openMenu("A100 Lab");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Who can use it" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("button", { name: "Org" })).toBeNull();
    await userEvent.click(within(dialog).getByLabelText("Team"));
    expect(await screen.findByRole("option", { name: "Data" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "ML" })).toBeNull();
    expect(screen.queryByRole("option", { name: "Acme" })).toBeNull();
    await userEvent.keyboard("{Escape}");
    await userEvent.click(within(dialog).getByRole("button", { name: "Member" }));
    await userEvent.click(within(dialog).getByLabelText("Member"));
    expect(await screen.findByRole("option", { name: "Grace Hopper" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Ada Lovelace" })).toBeNull();
    // The roster asked is the team's own, sub-teams included; the org's member list is never read.
    expect(server.sent.some((s) => s.path === "/api/v1/org/members")).toBe(false);
    expect(server.sent.some((s) => s.path === `/api/v1/teams/${TEAM_DATA}/members` && s.search.includes("include_descendants=true"))).toBe(true);
  });
});

describe("the default for new workspaces", () => {
  const LAB = "00000000-0000-4000-8000-0000000000m1";
  const RIG = "00000000-0000-4000-8000-0000000000m2";

  const field = () => screen.findByLabelText(COPY.defaultMachine);

  async function pick(name: string) {
    await userEvent.click(await field());
    await userEvent.click(await screen.findByRole("option", { name }));
  }

  beforeEach(() => {
    server.machines = [orgMachine(), orgMachine({ id: RIG, name: "Render rig", version: 1 })];
    server.computeSettings = { ...server.computeSettings, version: 4 };
  });

  it("lists the org's machines and none, showing the default the server holds", async () => {
    server.computeSettings = { ...server.computeSettings, default_org_machine_id: RIG };
    renderPage();
    await waitFor(async () => expect(await field()).toHaveTextContent("Render rig"));
    await userEvent.click(await field());
    const options = (await screen.findAllByRole("option")).map((o) => o.textContent);
    expect(options).toEqual([COPY.noDefaultMachine, "A100 Lab", "Render rig"]);
  });

  it("saves the machine picked with the settings version it read, and shows the server's answer", async () => {
    renderPage();
    await waitFor(async () => expect(await field()).toBeEnabled());
    await pick("A100 Lab");
    await waitFor(() => expect(server.calls("PUT", "/api/v1/org/compute/settings")).toHaveLength(1));
    const [put] = server.calls("PUT", "/api/v1/org/compute/settings");
    expect(put.body).toEqual({ default_org_machine_id: LAB });
    expect(put.ifMatch).toBe("4");
    await waitFor(async () => expect(await field()).toHaveTextContent("A100 Lab"));
    expect(server.computeSettings.default_org_machine_id).toBe(LAB);
  });

  it("clears the default with null when none is picked", async () => {
    server.computeSettings = { ...server.computeSettings, default_org_machine_id: LAB };
    renderPage();
    await waitFor(async () => expect(await field()).toHaveTextContent("A100 Lab"));
    await pick(COPY.noDefaultMachine);
    await waitFor(() => expect(server.calls("PUT", "/api/v1/org/compute/settings")).toHaveLength(1));
    expect(server.calls("PUT", "/api/v1/org/compute/settings")[0].body).toEqual({ default_org_machine_id: null });
    await waitFor(async () => expect(await field()).toHaveTextContent(COPY.noDefaultMachine));
  });

  it("shows the default another admin set when its write was built on an older reading", async () => {
    renderPage();
    await waitFor(async () => expect(await field()).toBeEnabled());
    // Another admin sets it after this page read the settings.
    server.computeSettings = { ...server.computeSettings, default_org_machine_id: RIG, version: 5 };
    await pick("A100 Lab");
    expect(await screen.findByText("This setting changed since you loaded it.")).toBeInTheDocument();
    await waitFor(async () => expect(await field()).toHaveTextContent("Render rig"));
    expect(server.computeSettings.default_org_machine_id).toBe(RIG);
  });

  it("reads none once the default machine is deleted", async () => {
    server.computeSettings = { ...server.computeSettings, default_org_machine_id: LAB };
    renderPage();
    await waitFor(async () => expect(await field()).toHaveTextContent("A100 Lab"));
    await openMenu("A100 Lab");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox"), "A100 Lab");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(async () => expect(await field()).toHaveTextContent(COPY.noDefaultMachine));
  });

  it("is not offered to anyone but an org admin, who is the only one who may read it", async () => {
    server.viewer = { isOrgAdmin: false, adminTeamIds: [TEAM_DATA] };
    renderPage();
    await row("A100 Lab");
    expect(screen.queryByLabelText(COPY.defaultMachine)).toBeNull();
    expect(server.sent.some((s) => s.path === "/api/v1/org/compute/settings")).toBe(false);
  });
});
