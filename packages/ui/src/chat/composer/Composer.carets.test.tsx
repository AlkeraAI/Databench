import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer, anchorCaret, type RemoteCaret } from "./Composer";

// Somebody else's caret in this field.
//
// A textarea cannot be drawn inside, so the carets ride a mirror: the same text,
// laid out the same way, with a zero-width marker spliced in at the offset. jsdom
// computes no layout at all, so what is asserted here is the only thing that
// matters for correctness and the only thing a browser cannot fix for us — WHICH
// CHARACTER the marker is spliced after. Get that wrong and no amount of correct
// CSS puts the bar in the right place.
//
// The re-anchoring is exercised as behaviour through the rendered mirror rather
// than only against the pure function, because the bug it exists to stop ("the
// draft merged and every caret is now a word to the left") is only visible there.

afterEach(cleanup);

function base() {
  return {
    modes: [{ value: "ask", label: "Ask" }],
    mode: "ask",
    models: [{ value: "m1", label: "M1" }],
    model: "m1",
    onModelChange: vi.fn(),
    efforts: [{ value: "low", label: "Low", bars: 1 as const }],
    effort: "low",
    onEffortChange: vi.fn(),
    onSend: vi.fn(),
  };
}

const field = (): HTMLTextAreaElement => screen.getByLabelText("Message Databench");

function caret(over: Partial<RemoteCaret> = {}): RemoteCaret {
  return { id: "peer-bo", name: "Bo Chen", hue: 200, offset: 0, anchor: 0, ...over };
}

function layers(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>(".chat-composer-mirror")];
}

/** The bar one peer's layer draws, and the text the mirror lays before it —
 *  which is exactly the offset the bar sits at. */
function barIn(layer: HTMLElement): { bar: HTMLElement; textBefore: string } {
  const bar = layer.querySelector<HTMLElement>(".chat-composer-caret");
  if (!bar) throw new Error("no caret bar in this layer");
  let textBefore = "";
  for (const node of layer.childNodes) {
    if (node === bar) return { bar, textBefore };
    if (node.contains(bar)) throw new Error("the bar is nested, not spliced");
    textBefore += node.textContent ?? "";
  }
  throw new Error("the bar is not a child of the mirror");
}

/** The draft as this layer lays it out, with the flag (which is chrome, not
 *  text) taken back out. */
function mirrorText(layer: HTMLElement): string {
  const copy = layer.cloneNode(true) as HTMLElement;
  for (const flag of copy.querySelectorAll(".chat-composer-caret__flag")) flag.remove();
  return (copy.textContent ?? "").replace(/​/g, "");
}

describe("the carets of the other people in this draft", () => {
  it("draws one bar per remote peer, named and coloured, and none for the reader", () => {
    // The host has already taken this reader's own peer out: the composer draws
    // everything it is handed, and the three-caret case is what would catch a
    // composer that had started filtering (or not filtering) on its own.
    render(
      <Composer
        {...base()}
        draft={{ text: "ship the report", at: 1 }}
        remoteCarets={[
          caret({ id: "peer-bo", name: "Bo Chen", hue: 200, offset: 5, anchor: 5 }),
          caret({ id: "peer-cleo", name: "Cleo Marsh", hue: 40, offset: 9, anchor: 9 }),
        ]}
      />,
    );
    const drawn = layers();
    expect(drawn.map((l) => l.dataset.caretPeer)).toEqual(["peer-bo", "peer-cleo"]);

    const bo = barIn(drawn[0]!);
    expect(bo.bar.dataset.caretName).toBe("Bo Chen");
    expect(bo.textBefore).toBe("ship ");
    expect(bo.textBefore.length).toBe(5);
    expect(drawn[0]!.style.getPropertyValue("--chat-caret-hue")).toBe("200");

    const cleo = barIn(drawn[1]!);
    expect(cleo.bar.dataset.caretName).toBe("Cleo Marsh");
    expect(cleo.textBefore).toBe("ship the ");
    expect(drawn[1]!.style.getPropertyValue("--chat-caret-hue")).toBe("40");

    // Both layers still spell the whole draft, so the mirror wraps exactly as
    // the field does rather than at some truncated text.
    for (const layer of drawn) expect(mirrorText(layer)).toBe("ship the report");
  });

  it("gives a peer who leaves nothing to draw with", () => {
    const view = render(
      <Composer
        {...base()}
        draft={{ text: "ship the report", at: 1 }}
        remoteCarets={[
          caret({ id: "peer-bo", offset: 5, anchor: 5 }),
          caret({ id: "peer-cleo", name: "Cleo Marsh", hue: 40, offset: 9, anchor: 9 }),
        ]}
      />,
    );
    expect(layers()).toHaveLength(2);
    view.rerender(
      <Composer
        {...base()}
        draft={{ text: "ship the report", at: 1 }}
        remoteCarets={[caret({ id: "peer-cleo", name: "Cleo Marsh", hue: 40, offset: 9, anchor: 9 })]}
      />,
    );
    expect(layers().map((l) => l.dataset.caretPeer)).toEqual(["peer-cleo"]);
    expect(screen.queryByText("Bo Chen")).toBeNull();
  });

  it("has no overlay at all where nobody else is in the draft", () => {
    // The editor's one-keyboard chat, and the case a host must not pay for.
    render(<Composer {...base()} draft={{ text: "ship the report", at: 1 }} />);
    expect(layers()).toHaveLength(0);
    expect(document.querySelector(".chat-composer-carets")).toBeNull();
  });

  it("keeps the overlay out of the pointer's and the reader's way", () => {
    render(
      <Composer
        {...base()}
        draft={{ text: "ship it", at: 1 }}
        remoteCarets={[caret({ offset: 4, anchor: 4 })]}
      />,
    );
    const overlay = document.querySelector<HTMLElement>(".chat-composer-carets");
    expect(overlay?.getAttribute("aria-hidden")).toBe("true");
    // Every part of it, the name flags included, is inside that one hidden box:
    // a caret read out as text would arrive in the middle of the sentence the
    // reader is writing.
    expect(screen.getByText("Bo Chen").closest("[aria-hidden='true']")).toBe(overlay);
    // And the field, not the overlay, is what a click lands on.
    expect(overlay?.contains(field())).toBe(false);
  });

  it("follows the text when a merge inserts above the caret", () => {
    // Bo's caret was reported at 5 against "ship the report". By the time the
    // frame lands, the draft has been merged with "Please " in front of it: the
    // raw offset would put the bar inside "Please", three characters short.
    const bo = caret({ offset: 5, anchor: 5, before: "ship ", after: "the" });
    const view = render(
      <Composer {...base()} draft={{ text: "ship the report", at: 1 }} remoteCarets={[bo]} />,
    );
    expect(barIn(layers()[0]!).textBefore).toBe("ship ");

    view.rerender(
      <Composer {...base()} draft={{ text: "Please ship the report", at: 2 }} remoteCarets={[bo]} />,
    );
    const moved = barIn(layers()[0]!);
    expect(moved.textBefore).toBe("Please ship ");
    expect(moved.textBefore.length).toBe(5 + "Please ".length);
  });

  it("draws a selection as a band with the bar at the end being dragged", () => {
    render(
      <Composer
        {...base()}
        draft={{ text: "ship the report", at: 1 }}
        remoteCarets={[caret({ offset: 8, anchor: 5, before: "p the", after: " report" })]}
      />,
    );
    const layer = layers()[0]!;
    expect(layer.querySelector(".chat-composer-caret__range")?.textContent).toBe("the");
    // Head at 8, anchor at 5 — dragged rightwards, so the bar closes the band.
    expect(barIn(layer).textBefore).toBe("ship the");
  });

  it("puts the bar at the start of a selection dragged backwards", () => {
    render(
      <Composer
        {...base()}
        draft={{ text: "ship the report", at: 1 }}
        remoteCarets={[caret({ offset: 5, anchor: 8 })]}
      />,
    );
    const layer = layers()[0]!;
    expect(barIn(layer).textBefore).toBe("ship ");
    expect(layer.querySelector(".chat-composer-caret__range")?.textContent).toBe("the");
  });

  it("clamps a caret past the end of a draft that shrank under it", () => {
    render(
      <Composer
        {...base()}
        draft={{ text: "ship", at: 1 }}
        remoteCarets={[caret({ offset: 40, anchor: 40, before: "gone", after: "" })]}
      />,
    );
    expect(barIn(layers()[0]!).textBefore).toBe("ship");
  });
});

describe("re-anchoring a caret against text that moved", () => {
  it("leaves a caret whose surroundings did not move exactly where it was", () => {
    expect(anchorCaret("ship the report", { offset: 5, anchor: 5, before: "ship ", after: "the" })).toEqual({
      offset: 5,
      anchor: 5,
    });
  });

  it("picks the occurrence nearest the reported offset when the text repeats", () => {
    // "ok. " three times: every copy matches the context, and only the distance
    // says which one the peer was actually in. A first-match search would answer 2.
    const text = "ok. ok. ok.";
    expect(anchorCaret(text, { offset: 9, anchor: 9, before: "ok", after: "." })).toEqual({
      offset: 10,
      anchor: 10,
    });
  });

  it("carries the other end of a selection by the same shift", () => {
    // "the" selected backwards in "ship the report": head at 5, anchor at 8.
    // "Please " lands in front of it, so BOTH ends move seven characters and
    // the peer still has exactly "the" selected.
    expect(
      anchorCaret("Please ship the report", { offset: 5, anchor: 8, before: "ship ", after: "the" }),
    ).toEqual({ offset: 12, anchor: 15 });
  });

  it("clamps rather than losing a caret whose context is gone", () => {
    expect(anchorCaret("short", { offset: 40, anchor: 40, before: "vanished", after: "text" })).toEqual({
      offset: 5,
      anchor: 5,
    });
  });

  it("holds a caret at the very start, where there is no text before it", () => {
    expect(anchorCaret("ship the report", { offset: 0, anchor: 0, before: "", after: "ship" })).toEqual({
      offset: 0,
      anchor: 0,
    });
  });
});

describe("what this reader's caret tells the host", () => {
  it("reports the offset and the text either side of it as the caret moves", async () => {
    const onSelectionChange = vi.fn();
    render(<Composer {...base()} onSelectionChange={onSelectionChange} />);
    await userEvent.type(field(), "ship it");

    const last = onSelectionChange.mock.calls.at(-1)?.[0];
    expect(last).toEqual({ offset: 7, anchor: 7, before: "ship it", after: "" });

    // A caret moved by the keyboard's own navigation reaches a field through
    // the document, not through the field: the composer has to be listening
    // there too or arrowing left goes unreported.
    onSelectionChange.mockClear();
    const el = field();
    el.focus();
    el.setSelectionRange(5, 7);
    document.dispatchEvent(new Event("selectionchange"));
    expect(onSelectionChange).toHaveBeenCalledWith({
      offset: 7,
      anchor: 5,
      before: "ship it",
      after: "",
    });

    // …and only while this field is the one holding the caret.
    onSelectionChange.mockClear();
    el.blur();
    document.dispatchEvent(new Event("selectionchange"));
    expect(onSelectionChange).not.toHaveBeenCalled();
  });

  it("carries at most the agreed window of context, however long the draft", async () => {
    const onSelectionChange = vi.fn();
    render(<Composer {...base()} onSelectionChange={onSelectionChange} />);
    const long = "x".repeat(50);
    await userEvent.type(field(), long);
    const last = onSelectionChange.mock.calls.at(-1)?.[0];
    expect(last.offset).toBe(50);
    expect(last.before).toHaveLength(32);
    expect(last.after).toBe("");
  });

  it("says nothing at all where the host is not listening", async () => {
    // A shell with one keyboard binds no handler, and the field must not start
    // doing selection bookkeeping on its behalf.
    const onDraftChange = vi.fn();
    render(<Composer {...base()} onDraftChange={onDraftChange} />);
    await userEvent.type(field(), "hi");
    expect(onDraftChange).toHaveBeenCalled();
    expect(field().value).toBe("hi");
  });
});

// ── Where the caret layer is painted ──────────────────────────────────────────
//
// jsdom lays nothing out, so this reads the sheet instead of the screen and does
// the two sums a browser would do. Both were wrong on the screen at once: the
// name flag rides ABOVE its bar, and a caret on the FIRST line put that flag
// above the overlay's own top edge, where the clip that keeps the mirror inside
// the field cut it in half. So the overlay reaches a flag's height above the
// field and the mirror is pushed back down by exactly that much — the text stays
// on the field's lines, and the flag has somewhere to be. The layer also has to
// say where it sits in the composer's stack: over the field it decorates, under
// the two menus that open across it.

describe("the caret layer's place in the composer", () => {
  const HERE = dirname(fileURLToPath(import.meta.url));
  const CSS = readFileSync(join(HERE, "composer.css"), "utf8");
  const TOKENS = readFileSync(join(HERE, "..", "theme", "tokens.css"), "utf8");

  /** The declarations of the last rule with this exact selector. Comments go
   *  first: they carry braces of their own and would split a rule in two. */
  const RULES = CSS.replace(/\/\*[\s\S]*?\*\//g, "");

  function rule(selector: string): Map<string, string> {
    const blocks = [...RULES.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(
      (m) => (m[1] ?? "").split("}").at(-1)?.trim() === selector,
    );
    const body = blocks.at(-1)?.[2];
    if (body === undefined) throw new Error(`no rule for ${selector}`);
    const declarations = new Map<string, string>();
    for (const line of body.split(";")) {
      const at = line.indexOf(":");
      if (at === -1) continue;
      declarations.set(line.slice(0, at).trim(), line.slice(at + 1).trim());
    }
    return declarations;
  }

  /** Token values this geometry is spent in, read from the sheet that declares
   *  them — a token retuned there has to keep the arithmetic below true. */
  const tokens = new Map(
    [...TOKENS.matchAll(/(--chat-[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1] as string, (m[2] ?? "").trim()]),
  );

  const carets = rule(".chat-root .chat-composer-carets");
  const mirror = rule(".chat-root .chat-composer-mirror");
  const field = rule(".chat-root .chat-composer-field");

  /** A length in px, resolving `var(--chat-…)` against the sheets and the one
   *  arithmetic form these rules use. */
  function px(value: string | undefined): number {
    if (value === undefined) throw new Error("no such declaration");
    const resolved = value.replace(/var\(\s*(--[\w-]+)\s*\)/g, (_all, name: string) => {
      const declared = carets.get(name) ?? tokens.get(name);
      if (declared === undefined) throw new Error(`undeclared ${name}`);
      return declared;
    });
    const calc = /^calc\((.+)\)$/.exec(resolved.trim());
    return (calc?.[1] ?? resolved)
      .split("*")
      .map((factor) => Number.parseFloat(factor.trim()))
      .reduce((a, b) => a * b, 1);
  }

  /** The first value of a shorthand — the TOP of an `inset` or a `padding` —
   *  taken whole, `calc(…)` and the spaces inside it included. */
  const first = (shorthand: string | undefined): string =>
    /^(calc\(.*?\)\)|\S+)/.exec((shorthand ?? "").trim())?.[0] ?? "";

  const flagHeight = px("var(--chat-caret-flag-h)");
  const overlayTop = px(first(carets.get("inset")));
  const padTop = px(first(field.get("padding")));

  it("keeps the mirrored text on the field's own first line", () => {
    // The overlay moved up; the mirror moves down by the same amount, or every
    // caret in the field is drawn a flag's height too high.
    expect(overlayTop + px(mirror.get("top"))).toBe(0);
    // And the mirror's inner air still matches the field's, or the drift is the
    // same bug one padding further in.
    expect(px(first(mirror.get("padding")))).toBe(padTop);
  });

  it("leaves room above the field for the name on a first-line caret", () => {
    // The overlay clips, which is what cut the flag off…
    expect(carets.get("overflow")).toBe("hidden");
    // …so the flag's top edge has to land inside it. A caret on line one sits a
    // padding below the mirror's top, and its flag a flag's height above that.
    expect(px(mirror.get("top")) + padTop - flagHeight).toBeGreaterThanOrEqual(0);
  });

  it("paints over the field it decorates and under the menus that cross it", () => {
    const layer = Number(carets.get("z-index"));
    const popover = Number(rule(".chat-root .chat-composer-pop").get("z-index"));
    const slash = Number(rule(".chat-root .chat-composer-slash").get("z-index"));
    expect(layer).toBeGreaterThan(0);
    expect(layer).toBeLessThan(Math.min(popover, slash));
  });
});
