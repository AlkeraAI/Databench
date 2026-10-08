import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { TextInput } from "./TextInput";

afterEach(cleanup);

/** The required mark is a visual on the label, separate from the control's `required`
 *  semantics: a form can drop the asterisk without loosening validation. */
describe("TextInput required mark", () => {
  it("shows the mark beside the label by default when the field is required", () => {
    render(<TextInput label="Email" required value="" onChange={() => undefined} />);
    expect(screen.getByLabelText(/email/i)).toBeRequired();
    expect(document.querySelector(".alk-field__req")).not.toBeNull();
  });

  it("drops the mark with requiredMark={false} while the control stays required", () => {
    render(<TextInput label="Email" required requiredMark={false} value="" onChange={() => undefined} />);
    expect(screen.getByLabelText(/email/i)).toBeRequired();
    expect(document.querySelector(".alk-field__req")).toBeNull();
  });

  it("never shows a mark on a field that is not required", () => {
    render(<TextInput label="Nickname" value="" onChange={() => undefined} />);
    expect(screen.getByLabelText(/nickname/i)).not.toBeRequired();
    expect(document.querySelector(".alk-field__req")).toBeNull();
  });
});
