import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Textarea } from "./Textarea";

// Root-prop contract (TextInput/Select parity): shelled, rootClassName/rootStyle ride the FieldShell
// wrapper; bare (no label/description/error) the textarea IS the root, so they land on it directly —
// never dropped.

afterEach(cleanup);

describe("Textarea root props", () => {
  it("applies rootClassName + rootStyle to the field wrapper when shelled", () => {
    render(<Textarea label="Notes" rootClassName="cell" rootStyle={{ flex: "1 1 240px" }} />);
    const shell = document.querySelector(".alk-field") as HTMLElement;
    expect(shell).toHaveClass("cell");
    expect(shell.style.flex).toBe("1 1 240px");
    // The control itself does not double-carry the root props when shelled.
    expect(document.querySelector(".alk-textarea")).not.toHaveClass("cell");
  });

  it("lands them on the textarea when bare, under a caller style", () => {
    render(
      <Textarea
        aria-label="Notes"
        rootClassName="cell"
        rootStyle={{ flex: "1 1 240px", minWidth: "0px" }}
        style={{ minWidth: "10px" }}
      />,
    );
    expect(document.querySelector(".alk-field")).toBeNull();
    const ta = document.querySelector(".alk-textarea") as HTMLElement;
    expect(ta).toHaveClass("cell");
    expect(ta.style.flex).toBe("1 1 240px");
    // The caller wins a conflict.
    expect(ta.style.minWidth).toBe("10px");
  });
});
