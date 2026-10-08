import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { useRowSelection } from "./useRowSelection";

// Tests for the @alkera/ui useRowSelection hook — the select-all + per-row bookkeeping a
// selectable Table would otherwise hand-roll. Driven through a tiny host (the hook's only real
// surface). Every id/label the host renders is named once (KEY / BTN / TID / rowTid) and shared with
// the assertions; the flags are read back as a typed number/boolean, so nothing is a copied literal.

afterEach(cleanup);

const KEY = { a: "a", b: "b", c: "c" } as const;
const THREE = [KEY.a, KEY.b, KEY.c];
const TWO = [KEY.a, KEY.b];
const NONE: string[] = [];

const BTN = { toggleAll: "toggle all", clear: "clear" };
const TID = { count: "count", allOn: "all-on", someOn: "some-on" };
const rowTid = (k: string) => `row-${k}`;

function Harness({ keys }: { keys: string[] }) {
  const sel = useRowSelection(keys);
  return (
    <div>
      <output data-testid={TID.count}>{sel.count}</output>
      <output data-testid={TID.allOn}>{String(sel.allOn)}</output>
      <output data-testid={TID.someOn}>{String(sel.someOn)}</output>
      <button onClick={sel.toggleAll}>{BTN.toggleAll}</button>
      <button onClick={sel.clear}>{BTN.clear}</button>
      {keys.map((k) => (
        <button key={k} data-testid={rowTid(k)} data-on={sel.isSelected(k) || undefined} onClick={() => sel.toggle(k)}>
          {k}
        </button>
      ))}
    </div>
  );
}

const readCount = () => Number(screen.getByTestId(TID.count).textContent);
const readAllOn = () => screen.getByTestId(TID.allOn).textContent === String(true);
const readSomeOn = () => screen.getByTestId(TID.someOn).textContent === String(true);
const row = (k: string) => screen.getByTestId(rowTid(k));
const clickToggleAll = (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole("button", { name: BTN.toggleAll }));

describe("useRowSelection", () => {
  it("starts empty — nothing selected, no flags", () => {
    render(<Harness keys={THREE} />);
    expect(readCount()).toBe(0);
    expect(readAllOn()).toBe(false);
    expect(readSomeOn()).toBe(false);
  });

  it("toggling one key selects it and reads as a partial selection", async () => {
    const user = userEvent.setup();
    render(<Harness keys={THREE} />);
    await user.click(row(KEY.a));
    expect(readCount()).toBe(1);
    expect(row(KEY.a)).toHaveAttribute("data-on");
    expect(readSomeOn()).toBe(true);
    expect(readAllOn()).toBe(false);
  });

  it("toggling the same key twice deselects it", async () => {
    const user = userEvent.setup();
    render(<Harness keys={TWO} />);
    await user.click(row(KEY.a));
    await user.click(row(KEY.a));
    expect(readCount()).toBe(0);
    expect(row(KEY.a)).not.toHaveAttribute("data-on");
  });

  it("toggleAll selects every current key, then clears them when already all-on", async () => {
    const user = userEvent.setup();
    render(<Harness keys={THREE} />);
    await clickToggleAll(user);
    expect(readCount()).toBe(THREE.length);
    expect(readAllOn()).toBe(true);
    expect(readSomeOn()).toBe(false);
    await clickToggleAll(user);
    expect(readCount()).toBe(0);
    expect(readAllOn()).toBe(false);
  });

  it("clear() drops the whole selection", async () => {
    const user = userEvent.setup();
    render(<Harness keys={TWO} />);
    await clickToggleAll(user);
    expect(readCount()).toBe(TWO.length);
    await user.click(screen.getByRole("button", { name: BTN.clear }));
    expect(readCount()).toBe(0);
  });

  it("allOn is false for an empty key set (never vacuously true)", () => {
    render(<Harness keys={NONE} />);
    expect(readAllOn()).toBe(false);
    expect(readCount()).toBe(0);
  });

  it("counts + flags reason over the CURRENT keys — narrowing the set narrows the tally", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Harness keys={THREE} />);
    await clickToggleAll(user);
    expect(readCount()).toBe(THREE.length);
    expect(readAllOn()).toBe(true);
    // The dropped key (in THREE but not TWO) stays selected internally but is no longer counted, and
    // the remaining keys are still all-on.
    rerender(<Harness keys={TWO} />);
    expect(readCount()).toBe(TWO.length);
    expect(readAllOn()).toBe(true);
    expect(readSomeOn()).toBe(false);
  });

  it("narrowing to a set only PARTLY selected reads as someOn, not allOn", async () => {
    // The asymmetric complement: select one key, then narrow to a set of two containing it. One of two
    // current keys is on → the header must read indeterminate (someOn), never all-on.
    const user = userEvent.setup();
    const { rerender } = render(<Harness keys={THREE} />);
    await user.click(row(KEY.a));
    rerender(<Harness keys={TWO} />);
    expect(readCount()).toBe(1);
    expect(readAllOn()).toBe(false);
    expect(readSomeOn()).toBe(true);
  });

  it("a selection for an ABSENT key is inert in the tally, even after it returns to the set", async () => {
    // Select the key that TWO omits, then narrow it out: it must not count while absent.
    const user = userEvent.setup();
    const { rerender } = render(<Harness keys={THREE} />);
    await user.click(row(KEY.c));
    expect(readCount()).toBe(1);
    rerender(<Harness keys={TWO} />);
    expect(readCount()).toBe(0);
    expect(readSomeOn()).toBe(false);
    expect(readAllOn()).toBe(false);
    // When it returns to the set, the held selection is live again — filtering never mutated the set.
    rerender(<Harness keys={THREE} />);
    expect(readCount()).toBe(1);
    expect(row(KEY.c)).toHaveAttribute("data-on");
  });

  it("toggleAll over a NARROWED set selects exactly the current keys, ignoring dropped ones", async () => {
    // With a stale selection still held from a wider view, toggling-all on the narrowed set must reason
    // over the CURRENT keys only. A naive impl that checked the whole set could mis-fire and clear.
    const user = userEvent.setup();
    const { rerender } = render(<Harness keys={THREE} />);
    await user.click(row(KEY.c)); // c is in THREE but not TWO
    rerender(<Harness keys={TWO} />);
    expect(readAllOn()).toBe(false);
    await clickToggleAll(user);
    expect(readCount()).toBe(TWO.length);
    expect(readAllOn()).toBe(true);
    for (const k of TWO) expect(row(k)).toHaveAttribute("data-on");
  });
});
