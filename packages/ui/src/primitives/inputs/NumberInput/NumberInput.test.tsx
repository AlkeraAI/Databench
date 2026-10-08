import { useState } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { NumberInput, sanitizeCash, sanitizeDigits, type NumberInputMode } from "./NumberInput";

// The numeric field over TextInput. `mode` picks the constraint; we assert what a user observes —
// the value the field settles on after typing, the affix, and the underlying input type — never a
// class. The pure sanitizers are pinned directly so the boundary cases don't need a keystroke each.

afterEach(cleanup);

function Harness({ mode, decimals }: { mode: NumberInputMode; decimals?: number }) {
  const [v, setV] = useState("");
  return <NumberInput mode={mode} decimals={decimals} label="Amount" value={v} onValueChange={setV} />;
}

describe("NumberInput entry constraints", () => {
  it.each([
    { mode: "integer" as const, decimals: undefined, typed: "12a3.5x", settles: "1235" },
    { mode: "cash" as const, decimals: undefined, typed: "12.999", settles: "12.99" },
    { mode: "cash" as const, decimals: undefined, typed: "9e9", settles: "99" },
    // A per-1M-token price needs the wider cap.
    { mode: "cash" as const, decimals: 4, typed: "0.03755", settles: "0.0375" },
  ])("$mode mode settles $typed on $settles", async ({ mode, decimals, typed, settles }) => {
    render(<Harness mode={mode} decimals={decimals} />);
    const input = screen.getByLabelText("Amount");
    await userEvent.type(input, typed);
    expect(input).toHaveValue(settles);
    if (mode === "cash") expect(screen.getByText("$")).toBeInTheDocument();
  });

  it("number mode is the permissive native numeric input", () => {
    render(<Harness mode="number" />);
    expect(screen.getByLabelText("Amount")).toHaveAttribute("type", "number");
  });
});

describe("the pure sanitizers", () => {
  it("sanitizeDigits keeps digits only", () => {
    expect(sanitizeDigits("1,234.5 kg")).toBe("12345");
    expect(sanitizeDigits("abc")).toBe("");
  });

  it("sanitizeCash keeps one decimal point and at most two fractional places", () => {
    expect(sanitizeCash("$1,299.999")).toBe("1299.99");
    expect(sanitizeCash("1.2.3")).toBe("1.23");
    expect(sanitizeCash("10")).toBe("10");
    expect(sanitizeCash("10.")).toBe("10.");
  });
});
