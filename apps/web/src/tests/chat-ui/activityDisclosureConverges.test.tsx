// A run reads the same whether its rows arrived while the reader watched or
// were folded from the log on a reload.
//
// The case is a Stop. The server writes the stop notice and stamps the turn
// over, and the aborted terminal's `tool.call_update` — the row the fold reads
// as a stopped call — lands a tenth of a second AFTER that. A reader watching
// therefore sees the run settle with no outcome on it and the stop arrive
// next; a reader who reloads folds both at once. The run's disclosure
// must not be able to tell those two apart: it is a function of what the fold
// holds, not of the order the rows reached this tab.
//
// Both readings are taken after the fold's conceal animation has run out, which
// is what a reader is looking at a second later — the departure keeps a closing
// run in the DOM for 220 ms, so comparing before that compares two animations.

import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Activity } from "@alkera/ui";
import type { CardStep } from "@alkera/ui";

const STOPPED = "1 stopped";
const COMMAND = "for i in $(seq 1 40); do echo $i; [ $i -lt 40 ] && sleep 1; done";
const SUMMARY = "Ran 1 terminal command";

/** The stopped bash call, at one of the three readings the log passes through.
 *  The id rides in because the disclosure memory is keyed on it and is module
 *  scope: two readings of the same run in one process would otherwise share
 *  the first one's fold, which a real reload never does. */
function bashStep(id: string, at: "running" | "settling" | "settled"): CardStep {
  return {
    id,
    verb: "Ran",
    object: COMMAND,
    objectKind: "command",
    loneSummary: SUMMARY,
    glyph: null,
    // The stop rides the tool row, which is the LAST of the three to land.
    status: at === "running" ? "running" : at === "settled" ? "stopped" : "error",
    body: <div>$ {COMMAND}</div>,
  };
}

/** Let every conceal run out, so what is read is the run at rest. */
function settle(): void {
  act(() => {
    vi.advanceTimersByTime(500);
  });
}

/** What a reader who reloads sees: the whole run folded from the log at once. */
function hydrated(id: string, at: "settling" | "settled"): string {
  const view = render(<Activity summary={SUMMARY} steps={[bashStep(id, at)]} folded />, {
    container: document.body.appendChild(document.createElement("div")),
  });
  settle();
  return view.container.textContent ?? "";
}

/** What a reader who was watching sees: the same rows, one render apart, in the
 *  order the server wrote them. `middle` is the tenth of a second between the
 *  turn ending and the aborted terminal landing. */
function watched(id: string, middle: "running" | "settling", last: "settling" | "settled"): string {
  const view = render(<Activity summary={SUMMARY} steps={[bashStep(id, "running")]} folded={false} />, {
    container: document.body.appendChild(document.createElement("div")),
  });
  // The stop: the box stamps the turn over before the tool row lands.
  view.rerender(<Activity summary={SUMMARY} steps={[bashStep(id, middle)]} folded />);
  settle();
  // The aborted terminal, 0.1 s later.
  view.rerender(<Activity summary={SUMMARY} steps={[bashStep(id, last)]} folded />);
  settle();
  return view.container.textContent ?? "";
}

describe("a stopped run reads the same live as it does after a reload", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  // Two spellings of the tenth of a second between the stop and the tool row,
  // because the fold may settle the call itself at the turn's end or leave it
  // running until its own row lands — and neither may change what is on screen
  // once both have arrived.
  for (const middle of ["running", "settling"] as const) {
    it(`shows the stop when the call was ${middle} at the stop`, () => {
      expect(watched(`live-${middle}`, middle, "settled")).toContain(STOPPED);
    });

    it(`reads identically to a reload when the call was ${middle} at the stop`, () => {
      const live = watched(`converge-live-${middle}`, middle, "settled");
      const reloaded = hydrated(`converge-reload-${middle}`, "settled");

      expect(live).toBe(reloaded);
    });
  }

  // The control, so the case above cannot pass by never folding anything: a run
  // that settles with no reason on it folds away, watched or reloaded.
  it("still folds a stopped run that carries no reason, live and reloaded alike", () => {
    const live = watched("quiet-live", "settling", "settling");
    const reloaded = hydrated("quiet-reload", "settling");

    expect(live).toBe(reloaded);
    expect(live).not.toContain("Show output");
    expect(live).not.toContain(COMMAND);
  });
});
