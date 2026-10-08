import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePager } from "./usePager";

// Tests for the usePager hook — the controlled paging state machine behind TablePager and
// DataTablePager: the >= 1 page-count clamp, the digit-only draft with blur/Enter commit, and the
// prev/next steppers with their bounds. Driven through a tiny host (the hook's only real surface);
// the components' own tests pin only their markup.

afterEach(cleanup);

const LABEL = { input: "page", prev: "prev", next: "next" };
const TID = { pages: "pages", current: "current" };

function Harness({ page, pageCount, onPage }: { page: number; pageCount: number; onPage: (p: number) => void }) {
  const pager = usePager({ page, pageCount, onPage });
  return (
    <div>
      <output data-testid={TID.pages}>{pager.pages}</output>
      <output data-testid={TID.current}>{pager.current}</output>
      <input aria-label={LABEL.input} {...pager.input} />
      <button aria-label={LABEL.prev} {...pager.prev} />
      <button aria-label={LABEL.next} {...pager.next} />
    </div>
  );
}

const input = () => screen.getByLabelText<HTMLInputElement>(LABEL.input);
const prev = () => screen.getByRole("button", { name: LABEL.prev });
const next = () => screen.getByRole("button", { name: LABEL.next });
const readPages = () => Number(screen.getByTestId(TID.pages).textContent);
const readCurrent = () => Number(screen.getByTestId(TID.current).textContent);

describe("usePager", () => {
  it("clamps an empty page count to one page with both steppers disabled", () => {
    render(<Harness page={1} pageCount={0} onPage={vi.fn()} />);
    expect(readPages()).toBe(1);
    expect(readCurrent()).toBe(1);
    expect(prev()).toBeDisabled();
    expect(next()).toBeDisabled();
  });

  it("clamps a committed page past the end back to the last page", () => {
    render(<Harness page={9} pageCount={3} onPage={vi.fn()} />);
    expect(readCurrent()).toBe(3);
    expect(input().value).toBe("3");
    expect(next()).toBeDisabled();
  });

  it("steps with prev / next from the clamped current page", async () => {
    const onPage = vi.fn();
    const { rerender } = render(<Harness page={1} pageCount={3} onPage={onPage} />);
    expect(prev()).toBeDisabled();
    await userEvent.click(next());
    expect(onPage).toHaveBeenLastCalledWith(2);

    rerender(<Harness page={3} pageCount={3} onPage={onPage} />);
    expect(next()).toBeDisabled();
    await userEvent.click(prev());
    expect(onPage).toHaveBeenLastCalledWith(2);
  });

  it("commits the typed draft on blur", async () => {
    const onPage = vi.fn();
    render(<Harness page={1} pageCount={5} onPage={onPage} />);
    await userEvent.clear(input());
    await userEvent.type(input(), "4");
    expect(onPage).not.toHaveBeenCalled(); // typing alone commits nothing
    await userEvent.tab();
    expect(onPage).toHaveBeenLastCalledWith(4);
  });

  it("commits on Enter, clamping the draft to [1, pages]", async () => {
    const onPage = vi.fn();
    render(<Harness page={2} pageCount={5} onPage={onPage} />);
    await userEvent.clear(input());
    await userEvent.type(input(), "99{Enter}");
    expect(onPage).toHaveBeenLastCalledWith(5);

    await userEvent.clear(input());
    await userEvent.type(input(), "0{Enter}");
    expect(onPage).toHaveBeenLastCalledWith(1);
  });

  it("refuses non-digit input and commits nothing from an empty draft", async () => {
    const onPage = vi.fn();
    render(<Harness page={2} pageCount={5} onPage={onPage} />);
    await userEvent.type(input(), "x");
    expect(input().value).toBe("2");
    await userEvent.clear(input());
    await userEvent.keyboard("{Enter}");
    expect(onPage).not.toHaveBeenCalled();
    // The abandoned draft falls back to mirroring the committed page.
    expect(input().value).toBe("2");
  });
});
