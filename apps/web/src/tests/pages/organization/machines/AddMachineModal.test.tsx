import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SshMachineTestRead } from "@/api/machines";
import { MachinesPage } from "@/pages/organization/machines/MachinesPage";
import { COPY } from "@/pages/organization/machines/model";
import { ADD_COPY } from "@/pages/organization/machines/AddMachineModal";

import { json, machinesServer, orgMachine, refuse, removeTopbar, renderAt, type MachinesServer } from "./machinesServer";

// Adding a machine the org runs, through the Machines page's real hooks: the
// entry point follows the server's verdict, Add waits for a passing test, the
// add names the fingerprint the reader was shown, and editing the connection
// clears the test.

let server: MachinesServer;
beforeEach(() => {
  server = machinesServer();
  server.viewer = { ...server.viewer, canAdd: true };
});
afterEach(() => {
  cleanup();
  removeTopbar();
  vi.unstubAllGlobals();
});

const PATH = "/settings/organization/machines";
const TEST_PATH = "/api/v1/org/machines/ssh/test";
const ADD_PATH = "/api/v1/org/machines/ssh";

const passing: SshMachineTestRead = {
  reachable: true,
  host_key_fingerprint: "SHA256:abc123",
  host_key_type: "ED25519",
  os: "Linux",
  arch: "x86_64",
  vcpu: 8,
  memory_gb: 31,
  disk_gb: 200,
  gpu_count: 0,
  prerequisites_met: true,
  missing: [],
  error_code: null,
  message: null,
};

async function openAndFill() {
  renderAt(<MachinesPage />, PATH);
  const [add] = await screen.findAllByRole("button", { name: COPY.add });
  await userEvent.click(add);
  await userEvent.type(screen.getByLabelText(/^Name/), "Basement rack");
  await userEvent.type(screen.getByLabelText(/^Host/), "10.0.0.5");
  await userEvent.type(screen.getByLabelText(/^Username/), "root");
  await userEvent.click(screen.getByRole("radio", { name: "Password" }));
  await userEvent.type(screen.getByLabelText(/^Password/), "s3cret");
}

describe("adding a machine the org runs", () => {
  it("offers Add only when the server says the reader may add", async () => {
    server.viewer = { ...server.viewer, canAdd: false };
    renderAt(<MachinesPage />, PATH);
    await screen.findByText("A100 Lab", { selector: ".alk-machine__name" });
    expect(screen.queryByRole("button", { name: COPY.add })).toBeNull();
  });

  it("tests, shows the fingerprint, and adds with the fingerprint it showed", async () => {
    server.writes[`POST ${TEST_PATH}`] = () => json(passing);
    server.writes[`POST ${ADD_PATH}`] = () => json(orgMachine({ id: "m-new", name: "Basement rack", acquisition: "added" }), 202);
    await openAndFill();
    const addButton = () => screen.getAllByRole("button", { name: ADD_COPY.submit }).at(-1)!;
    expect(addButton()).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: ADD_COPY.test }));
    expect(await screen.findByText("ED25519 SHA256:abc123")).toBeInTheDocument();
    expect(screen.getByText("Linux x86_64 · 8 vCPU · 31 GB memory · 200 GB disk")).toBeInTheDocument();
    await waitFor(() => expect(addButton()).toBeEnabled());
    await userEvent.click(addButton());
    await waitFor(() => expect(server.calls("POST", ADD_PATH)).toHaveLength(1));
    const [sent] = server.calls("POST", ADD_PATH);
    expect(sent.body).toMatchObject({
      name: "Basement rack",
      host: "10.0.0.5",
      port: 22,
      username: "root",
      auth_kind: "password",
      password: "s3cret",
      private_key: null,
      host_key_fingerprint: "SHA256:abc123",
    });
  });

  it("shows the fingerprint alone when the server names no key type", async () => {
    server.writes[`POST ${TEST_PATH}`] = () => json({ ...passing, host_key_type: null });
    await openAndFill();
    await userEvent.click(screen.getByRole("button", { name: ADD_COPY.test }));
    expect(await screen.findByText("SHA256:abc123")).toBeInTheDocument();
  });

  it("clears the test when a connection field changes, so Add waits for a new one", async () => {
    server.writes[`POST ${TEST_PATH}`] = () => json(passing);
    await openAndFill();
    await userEvent.click(screen.getByRole("button", { name: ADD_COPY.test }));
    await screen.findByText("ED25519 SHA256:abc123");
    await userEvent.type(screen.getByLabelText(/^Host/), "1");
    expect(screen.queryByText("ED25519 SHA256:abc123")).toBeNull();
    expect(screen.getAllByRole("button", { name: ADD_COPY.submit }).at(-1)).toBeDisabled();
  });

  it("keeps Add off for a host missing a prerequisite and names what it lacks", async () => {
    server.writes[`POST ${TEST_PATH}`] = () =>
      json({ ...passing, prerequisites_met: false, missing: ["systemd", "root or passwordless sudo"] });
    await openAndFill();
    await userEvent.click(screen.getByRole("button", { name: ADD_COPY.test }));
    expect(await screen.findByText("The host needs systemd, root or passwordless sudo.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: ADD_COPY.submit }).at(-1)).toBeDisabled();
  });

  it("keeps Add off and says why when the host could not reach this server back", async () => {
    const message =
      "Machines are told to reach this server at http://localhost:18880, which on the host is the host itself. Set ALKERA_NODE_API_URL to an address the host can reach.";
    server.writes[`POST ${TEST_PATH}`] = () =>
      json({ ...passing, prerequisites_met: false, error_code: "callback_unreachable", message });
    await openAndFill();
    await userEvent.click(screen.getByRole("button", { name: ADD_COPY.test }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: ADD_COPY.submit }).at(-1)).toBeDisabled();
  });

  it("says why a host could not be reached", async () => {
    server.writes[`POST ${TEST_PATH}`] = () =>
      json({ ...passing, reachable: false, host_key_fingerprint: null, error_code: "auth_failed", message: "The host refused the username or the credential." });
    await openAndFill();
    await userEvent.click(screen.getByRole("button", { name: ADD_COPY.test }));
    expect(await screen.findByText("The host refused the username or the credential.")).toBeInTheDocument();
  });

  it("shows the server's refusal when the host presents another key at add time", async () => {
    server.writes[`POST ${TEST_PATH}`] = () => json(passing);
    server.writes[`POST ${ADD_PATH}`] = () =>
      refuse(409, "host_key_mismatch", "The host presented a different host key than the one recorded (SHA256:zzz).");
    await openAndFill();
    await userEvent.click(screen.getByRole("button", { name: ADD_COPY.test }));
    await screen.findByText("ED25519 SHA256:abc123");
    await userEvent.click(screen.getAllByRole("button", { name: ADD_COPY.submit }).at(-1)!);
    expect(await screen.findByText(/different host key/)).toBeInTheDocument();
  });
});
