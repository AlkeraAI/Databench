import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TablePager, TABLE_PAGER_LABELS } from "./TablePager";

// The standalone footer navigation `Table` mounts (and non-table ledgers reuse). The paging state
// machine (clamping, the draft, digit filtering) is usePager's contract, pinned in
// src/hooks/usePager.test.tsx; this file pins the component's anatomy — the labeled input, the
// "of N" text, and the wired steppers.

afterEach(cleanup);

const input = () => screen.getByLabelText<HTMLInputElement>(TABLE_PAGER_LABELS.pageInput);
const prev = () => screen.getByRole("button", { name: TABLE_PAGER_LABELS.previous });
const next = () => screen.getByRole("button", { name: TABLE_PAGER_LABELS.next });

describe("TablePager", () => {
  it("names the current page and the page count", () => {
    render(<TablePager page={3} pageCount={7} onPage={vi.fn()} />);
    expect(input().value).toBe("3");
    expect(screen.getByText(/of 7/)).toBeInTheDocument();
  });

  it("steps with prev / next and disables each at its end", async () => {
    const onPage = vi.fn();
    const { rerender } = render(<TablePager page={1} pageCount={3} onPage={onPage} />);
    expect(prev()).toBeDisabled();
    await userEvent.click(next());
    expect(onPage).toHaveBeenLastCalledWith(2);

    rerender(<TablePager page={3} pageCount={3} onPage={onPage} />);
    expect(next()).toBeDisabled();
    await userEvent.click(prev());
    expect(onPage).toHaveBeenLastCalledWith(2);
  });
});
