// The composer's slash menu has two doors and one open state. Typing "/" filters
// by the token after it; the rail's key opens the same menu on an empty filter
// and never writes to the message. The field's value across a rail press is the
// contract that matters: an earlier build opened the menu by putting "/" in the
// textarea, which stole the user's draft.
//
// Enter is the only key that can run a command, and only against a row it can
// trust -- the one match the filter left, or the row the arrows walked to. A
// prefix several commands still share fires nothing, and a command that needs an
// argument completes instead. Tab always completes.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Composer, type ComposerProps } from "@alkera/ui";

const MODES = [
  { value: "plan", label: "Plan" },
  { value: "build", label: "Build" },
];
const MODELS = [
  { value: "model-one", label: "Model one" },
  { value: "model-two", label: "Model two" },
];
const EFFORTS = [
  { value: "low", label: "Low", bars: 1 as const },
  { value: "high", label: "High", bars: 3 as const },
];
// Two names share the "co" prefix and two do not, so a filter assertion proves
// exclusion, not just presence.
const COMMAND_NAMES = ["/clear", "/compact", "/context", "/model"];
// The CLI writes a required argument as "(name)" and an optional one as "[...]".
// One fixture carries a required one, the shape that cannot run bare.
const NEEDS_ARGUMENT = "/model";
const COMMANDS = COMMAND_NAMES.map((command) => ({
  command,
  summary: `Fixture summary for ${command}`,
  usage: command === NEEDS_ARGUMENT ? "(name)" : "",
}));

function renderComposer(overrides: Partial<ComposerProps> = {}): ReturnType<typeof render> {
  return render(
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
        slashCommands={COMMANDS}
        onSend={() => {}}
        {...overrides}
      />
    </div>,
  );
}

/** The composer's one writing field. */
const field = (): HTMLElement => screen.getByRole("textbox");

/** The rail's slash key, the pointer's door to the menu. */
function slashKey(): HTMLElement {
  const key = document.querySelector<HTMLElement>(".chat-composer-slashkey");
  if (!key) throw new Error("the rail has no slash key");
  return key;
}

const menu = (): HTMLElement | null => document.querySelector<HTMLElement>(".chat-composer-slash");

/** The commands the open menu offers, in order. Empty when it is closed. */
function listed(): string[] {
  const open = menu();
  if (!open) return [];
  return within(open)
    .getAllByRole("option")
    .map((row) => row.querySelector(".chat-composer-slash__cmd")?.textContent ?? "");
}

function option(command: string): HTMLElement {
  const open = menu();
  if (!open) throw new Error("the slash menu is closed");
  const row = within(open)
    .getAllByRole("option")
    .find((candidate) => candidate.querySelector(".chat-composer-slash__cmd")?.textContent === command);
  if (!row) throw new Error(`the slash menu offers no ${command}`);
  return row;
}

describe("composer slash menu", () => {
  it("opens from the rail key without writing to the message", async () => {
    const user = userEvent.setup();
    renderComposer();
    await user.type(field(), "explain this repo");

    await user.click(slashKey());

    // An earlier build opened the menu by typing "/" into the field.
    expect(field()).toHaveValue("explain this repo");
    expect(slashKey()).toHaveAttribute("aria-expanded", "true");
    expect(listed()).toEqual(COMMAND_NAMES);
  });

  it("dismisses on a second press of the rail key", async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.click(slashKey());
    expect(menu()).not.toBeNull();

    await user.click(slashKey());

    expect(menu()).toBeNull();
    expect(slashKey()).toHaveAttribute("aria-expanded", "false");
    expect(field()).toHaveValue("");
  });

  it("opens on a typed / and filters on the token after it", async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.type(field(), "/");
    expect(listed()).toEqual(COMMAND_NAMES);

    await user.type(field(), "co");
    expect(field()).toHaveValue("/co");
    expect(listed()).toEqual(["/compact", "/context"]);
  });

  it("runs the picked command through either door", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    const typed = renderComposer({ onSend });
    await user.type(field(), "/co");
    await user.click(option("/compact"));
    expect(onSend).toHaveBeenCalledExactlyOnceWith("/compact");
    expect(field()).toHaveValue("");
    expect(menu()).toBeNull();
    typed.unmount();

    // A command that needs an argument completes wherever it is picked.
    renderComposer({ onSend });
    await user.click(slashKey());
    await user.click(option(NEEDS_ARGUMENT));
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(field()).toHaveValue(`${NEEDS_ARGUMENT} `);
    expect(menu()).toBeNull();
  });

  it("closes when the message stops being a command", async () => {
    const user = userEvent.setup();
    renderComposer();
    await user.click(slashKey());
    expect(menu()).not.toBeNull();

    await user.type(field(), "hello");

    expect(menu()).toBeNull();
    expect(field()).toHaveValue("hello");
  });
});

describe("composer slash menu keys", () => {
  it("runs a no-argument command on one Enter", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });

    await user.type(field(), "/compact");
    await user.keyboard("{Enter}");

    // One Enter sends the command; it must not park "/compact " in the field
    // and wait for a second Enter.
    expect(onSend).toHaveBeenCalledExactlyOnceWith("/compact");
    expect(field()).toHaveValue("");
    expect(menu()).toBeNull();
  });

  it("completes a command that needs an argument, with the caret after it", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });

    await user.type(field(), NEEDS_ARGUMENT);
    await user.keyboard("{Enter}");

    expect(onSend).not.toHaveBeenCalled();
    expect(field()).toHaveValue(`${NEEDS_ARGUMENT} `);
    const box = field() as HTMLTextAreaElement;
    expect(box.selectionStart).toBe(box.value.length);
    expect(menu()).toBeNull();
  });

  it("fires nothing while several commands still match", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });

    await user.type(field(), "/c");
    await user.keyboard("{Enter}");

    expect(onSend).not.toHaveBeenCalled();
    expect(field()).toHaveValue("/c");
    expect(listed()).toEqual(["/clear", "/compact", "/context"]);
  });

  it("runs the row the arrows walked to", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });

    await user.type(field(), "/c");
    await user.keyboard("{ArrowDown}{Enter}");

    expect(onSend).toHaveBeenCalledExactlyOnceWith("/compact");
    expect(field()).toHaveValue("");
  });

  it("forgets the walked row once typing narrows the list again", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });

    await user.type(field(), "/c");
    await user.keyboard("{ArrowDown}o{Enter}");

    // The highlight went back to the first row, which the user never chose.
    expect(onSend).not.toHaveBeenCalled();
    expect(listed()).toEqual(["/compact", "/context"]);
  });

  it("opens the next menu unarmed after a command runs", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });
    await user.type(field(), "/c");
    await user.keyboard("{ArrowDown}{Enter}");
    onSend.mockClear();

    await user.type(field(), "/");
    await user.keyboard("{Enter}");

    expect(onSend).not.toHaveBeenCalled();
    expect(listed()).toEqual(COMMAND_NAMES);
  });

  it("completes on Tab and runs nothing", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });

    await user.type(field(), "/compact");
    await user.keyboard("{Tab}");

    expect(onSend).not.toHaveBeenCalled();
    expect(field()).toHaveValue("/compact ");
    expect(menu()).toBeNull();
  });

  it("closes on Escape without sending", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });

    await user.type(field(), "/compact");
    await user.keyboard("{Escape}");

    expect(menu()).toBeNull();
    expect(onSend).not.toHaveBeenCalled();
    expect(field()).toHaveValue("/compact");
  });

  it("sends an ordinary message on Enter with the menu closed", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ onSend });

    await user.type(field(), "explain this repo");
    await user.keyboard("{Enter}");

    expect(onSend).toHaveBeenCalledExactlyOnceWith("explain this repo");
    expect(field()).toHaveValue("");
  });
});
