import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GROW_COPY, growDone, growTitle } from "@/pages/organization/machines/GrowDiskDialog";
import { MachinesPage } from "@/pages/organization/machines/MachinesPage";

import { machinesServer, orgMachine, removeTopbar, renderAt, type MachinesServer } from "./machinesServer";

// Grow disk from a machine's menu, through the real hooks and the real
// mutation policy: the sizes and the restart note are the
// server's, and a grow sends the size with the version the reader saw.

let server: MachinesServer;
beforeEach(() => {
  server = machinesServer();
  server.machines = [orgMachine({ disk_grow: { min_gb: 201, max_gb: 1000, restarts: true } })];
});
afterEach(() => {
  cleanup();
  removeTopbar();
  vi.unstubAllGlobals();
});

const renderPage = () => renderAt(<MachinesPage />, "/settings/organization/machines");

async function openGrow() {
  await userEvent.click(await screen.findByRole("button", { name: "Actions for A100 Lab" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: GROW_COPY.confirm }));
  return screen.findByRole("dialog", { name: growTitle("A100 Lab") });
}

describe("growing a disk", () => {
  it("is offered only where the server offers it", async () => {
    server.machines = [orgMachine({ disk_grow: null })];
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Actions for A100 Lab" }));
    expect(screen.queryByRole("menuitem", { name: GROW_COPY.confirm })).toBeNull();
  });

  it("says the machine restarts and grows it with the version read", async () => {
    renderPage();
    const dialog = await openGrow();
    expect(within(dialog).getByText("Between 201 and 1000 GB.")).toBeInTheDocument();
    expect(within(dialog).getByText(GROW_COPY.restarts)).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: GROW_COPY.confirm });
    expect(confirm).toBeDisabled();

    await userEvent.type(within(dialog).getByLabelText(GROW_COPY.label), "400");
    await waitFor(() => expect(confirm).toBeEnabled());
    await userEvent.click(confirm);

    await waitFor(() => expect(server.calls("POST", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1/disk")).toHaveLength(1));
    const [sent] = server.calls("POST", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1/disk");
    expect(sent.body).toEqual({ volume_gb: 400 });
    expect(sent.ifMatch).toBe("3");
    expect(await screen.findByText(growDone("A100 Lab", 400))).toBeInTheDocument();
  });

  it("names no price, even for a priced quote, with nothing installed that prices machines", async () => {
    server.priced = true;
    renderPage();
    const dialog = await openGrow();
    await userEvent.type(within(dialog).getByLabelText(GROW_COPY.label), "400");
    const confirm = within(dialog).getByRole("button", { name: GROW_COPY.confirm });
    // The quote answered: the grow is admitted, and nothing names a charge.
    await waitFor(() => expect(confirm).toBeEnabled());
    expect(within(dialog).queryByTestId("grow-price")).toBeNull();
    expect(within(dialog).queryByText(/\/day/)).toBeNull();
  });

  it("says nothing of a restart where the disk grows in place", async () => {
    server.machines = [orgMachine({ disk_grow: { min_gb: 201, max_gb: 1000, restarts: false } })];
    renderPage();
    const dialog = await openGrow();
    expect(within(dialog).queryByText(GROW_COPY.restarts)).toBeNull();
  });

  it("refuses a size outside the server's range in the server's range words", async () => {
    renderPage();
    const dialog = await openGrow();
    await userEvent.type(within(dialog).getByLabelText(GROW_COPY.label), "100");
    expect(await within(dialog).findByText("Between 201 and 1000 GB.")).toBeInTheDocument();
    expect(within(dialog).queryByTestId("grow-price")).toBeNull();
    expect(within(dialog).getByRole("button", { name: GROW_COPY.confirm })).toBeDisabled();
  });

  it("holds the grow when the server says the credit cannot carry the bigger disk", async () => {
    server.quoteVerdict = { verdict: "refused", code: "insufficient_credit", message: "Not enough credit to run this machine for 1 hour." };
    renderPage();
    const dialog = await openGrow();
    await userEvent.type(within(dialog).getByLabelText(GROW_COPY.label), "400");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Not enough credit to run this machine for 1 hour.");
    expect(within(dialog).getByRole("button", { name: GROW_COPY.confirm })).toBeDisabled();
    expect(server.calls("POST", "/api/v1/org/machines/00000000-0000-4000-8000-0000000000m1/disk")).toHaveLength(0);
  });
});
