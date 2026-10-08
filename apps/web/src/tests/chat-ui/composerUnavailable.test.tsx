// The composer's `unavailable` pair, and the promise that it changes nothing
// where it is not passed.
//
// The browser's chat has a state the editor's does not: the workspace machine
// is starting, or is not answering, so there is nothing to send TO. The unit
// stays in place, disabled, saying why. That is a browser concern — the
// extension's daemon IS the machine — so the extension never passes either prop,
// and the parity requirement is that an absent prop leaves the
// extension's composer byte-for-byte what it was: a live field with its own
// placeholder and a send key that wakes on typed text.

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Composer, type ComposerProps } from "@alkera/ui";

const MODES = [
  { value: "plan", label: "Plan" },
  { value: "build", label: "Build" },
];
const MODELS = [{ value: "model-one", label: "Model one" }];
const EFFORTS = [{ value: "low", label: "Low", bars: 1 as const }];

function renderComposer(overrides: Partial<ComposerProps> = {}): void {
  render(
    <div className="chat-root">
      <Composer
        modes={MODES}
        mode="plan"
        onModeChange={() => {}}
        models={MODELS}
        model="model-one"
        onModelChange={() => {}}
        efforts={EFFORTS}
        effort="low"
        onEffortChange={() => {}}
        slashCommands={[]}
        onSend={() => {}}
        {...overrides}
      />
    </div>,
  );
}

const field = (): HTMLTextAreaElement => screen.getByRole("textbox") as HTMLTextAreaElement;
const sendKey = (): HTMLButtonElement => {
  const key = document.querySelector<HTMLButtonElement>(".chat-composer-send");
  if (!key) throw new Error("the composer has no send key");
  return key;
};

describe("the extension's composer, with neither new prop passed", () => {
  it("keeps a live field carrying its own placeholder", () => {
    renderComposer({ placeholder: "Ask Alkera" });
    expect(field().disabled).toBe(false);
    expect(field().placeholder).toBe("Ask Alkera");
  });

  it("still sends what the reader types", async () => {
    const onSend = vi.fn();
    renderComposer({ onSend });
    await userEvent.type(field(), "how many orders yesterday?{Enter}");
    expect(onSend.mock.calls[0]?.[0]).toBe("how many orders yesterday?");
  });

  it("renders the same markup as passing the pair off explicitly", () => {
    renderComposer();
    const absent = document.querySelector(".chat-root")?.innerHTML;
    document.body.innerHTML = "";
    renderComposer({ unavailable: false, unavailableReason: undefined });
    expect(document.querySelector(".chat-root")?.innerHTML).toBe(absent);
  });
});

describe("the browser's composer, while its machine is not ready", () => {
  it("disables the field and says why in place of the placeholder", () => {
    renderComposer({
      unavailable: true,
      unavailableReason: "Starting your workspace…",
      placeholder: "Ask Alkera",
    });
    expect(field().disabled).toBe(true);
    expect(field().placeholder).toBe("Starting your workspace…");
  });

  it("kills the send key that typed text would otherwise wake", async () => {
    const onSend = vi.fn();
    renderComposer({ onSend });
    await userEvent.type(field(), "a question");
    // Live, with a draft in the field: the key is pressable.
    expect(sendKey().disabled).toBe(false);
    document.body.innerHTML = "";
    // Unavailable: dead regardless of what is in the field.
    renderComposer({ onSend, unavailable: true, unavailableReason: "Not answering" });
    expect(sendKey().disabled).toBe(true);
    await userEvent.click(sendKey());
    expect(onSend).not.toHaveBeenCalled();
  });

  it("falls back to the placeholder when no reason is given", () => {
    renderComposer({ unavailable: true, placeholder: "Ask Alkera" });
    expect(field().placeholder).toBe("Ask Alkera");
  });
});
