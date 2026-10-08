// With nothing registered to price machines, a card names no rate, whatever rate the
// server sends.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { machineRateText } from "./format";
import { MachineCardView, machineSpecParts, type MachineCardData } from "./MachineCardView";

afterEach(cleanup);

const CARD: MachineCardData = {
  kind: "org_machine",
  org_machine_id: "om-1",
  name: "Lab",
  spec: {
    offering_name: "Box",
    provider: "localdev",
    region: "",
    gpu: null,
    vcpu: 4,
    memory_gb: 8,
    disk_gb: 50,
    rate_per_minute_nanos: 31_500_000,
    storage_rate_per_minute_nanos: 100_000,
  },
  state: "running",
};

describe("a card where nothing prices machines", () => {
  it("names the hardware and no rate", () => {
    expect(machineRateText(31_500_000)).toBeNull();
    expect(machineSpecParts(CARD)).toEqual(["4 vCPU", "8 GB memory", "50 GB disk", "Local box"]);
    render(<MachineCardView card={CARD} facts="rate" />);
    expect(screen.getByText("4 vCPU · 8 GB memory · 50 GB disk · Local box")).toBeInTheDocument();
  });
});
