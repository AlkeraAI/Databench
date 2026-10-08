import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BlobReference } from "@alkera/chat-model";

import { ReferenceStoreProvider, useReferenceActions } from "./ReferenceStoreProvider";

afterEach(cleanup);

const REF: BlobReference = { handle: "sha-1", name: "Q3 revenue" };

/** A consumer that reads onOpen from the store and counts its own mounts, so a
 *  test can prove a changed callback arrives WITHOUT remounting the subtree. */
function Probe({ mounts }: { mounts: { count: number } }) {
  const mounted = useRef(false);
  if (!mounted.current) {
    mounted.current = true;
    mounts.count += 1;
  }
  const onOpen = useReferenceActions((state) => state.onOpen);
  return (
    <button type="button" disabled={!onOpen} onClick={onOpen ? () => onOpen(REF) : undefined}>
      open
    </button>
  );
}

describe("ReferenceStoreProvider", () => {
  it("seeds the store for the first paint", () => {
    const onOpen = vi.fn();
    const mounts = { count: 0 };
    render(
      <ReferenceStoreProvider actions={{ onOpen }}>
        <Probe mounts={mounts} />
      </ReferenceStoreProvider>,
    );
    // Enabled from the very first render — no effect-lag frame where the chip
    // flashes disabled.
    expect(screen.getByRole("button", { name: "open" })).toBeEnabled();
  });

  it("a changed host callback reaches the consumer without remounting it", () => {
    const first = vi.fn();
    const second = vi.fn();
    const mounts = { count: 0 };
    const { rerender } = render(
      <ReferenceStoreProvider actions={{ onOpen: first }}>
        <Probe mounts={mounts} />
      </ReferenceStoreProvider>,
    );
    rerender(
      <ReferenceStoreProvider actions={{ onOpen: second }}>
        <Probe mounts={mounts} />
      </ReferenceStoreProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "open" }));
    expect(second).toHaveBeenCalledWith(REF);
    expect(first).not.toHaveBeenCalled();
    expect(mounts.count).toBe(1);
  });

  it("a consumer with no actions reads undefined", () => {
    const onOpen = vi.fn();
    const mounts = { count: 0 };
    const { rerender, unmount } = render(
      <ReferenceStoreProvider actions={{ onOpen }}>
        <Probe mounts={mounts} />
      </ReferenceStoreProvider>,
    );
    expect(screen.getByRole("button", { name: "open" })).toBeEnabled();

    // Replace semantics, not merge: the stale onOpen must NOT survive.
    rerender(
      <ReferenceStoreProvider actions={undefined}>
        <Probe mounts={mounts} />
      </ReferenceStoreProvider>,
    );
    expect(screen.getByRole("button", { name: "open" })).toBeDisabled();
    unmount();

    render(<Probe mounts={{ count: 0 }} />);
    expect(screen.getByRole("button", { name: "open" })).toBeDisabled();
  });
});
