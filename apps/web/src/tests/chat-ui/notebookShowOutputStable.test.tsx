// A shown notebook output holds still while the transcript around it changes.
//
// Every transcript update hands the card a new tool part: the publisher
// re-sends it, the output is the same JSON parsed again, and a message
// streams in beside it. The drawn chart must be the same SVG node through all
// of it (a redraw empties the chart's view and draws it again, which is the
// flicker a reader sees), and the figure must never fall back to loading.

import { render, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";
import { Activity } from "@alkera/ui";

import { stepOf, toolPart } from "./_steps";

beforeAll(() => {
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
});

const SPEC = {
  $schema: "https://vega.github.io/schema/vega-lite/v6.json",
  mark: "bar",
  encoding: { x: { field: "region", type: "nominal" }, y: { field: "revenue", type: "quantitative" } },
  data: { name: "sales" },
  datasets: { sales: [{ region: "north", revenue: 3 }, { region: "south", revenue: 4 }] },
};
const OUTPUT = JSON.stringify({
  path: "roi.alknb.py",
  cell_id: "a7yg9x7evz",
  cell_name: "visibility_roi_chart",
  kind: "chart",
  available: ["chart"],
  chart_spec: { untrusted: true, author: "Agent", content: SPEC },
  note: "",
});

/** The same call as a new transcript update delivers it: a fresh part object
 *  whose output is the same text. */
function shown(): ToolConversationPart {
  return { ...toolPart("alkera_notebook.show_output", { input: { path: "/home/alkera/roi.alknb.py" }, output: OUTPUT }), id: "p-show", callId: "c-show" };
}

function streaming(text: string): ToolConversationPart {
  return { ...toolPart("bash", { input: { command: `echo ${text}` }, output: text, state: "running" }), id: "p-bash", callId: "c-bash" };
}

function transcript(updates: number) {
  const steps = [stepOf(shown()), stepOf(streaming("x".repeat(updates)))];
  return (
    <div className="chat-root">
      <Activity steps={steps} summary="Ran 2 tools" />
    </div>
  );
}

describe("a notebook chart shown in the chat", () => {
  it("is drawn once and stays the same node while the transcript streams around it", async () => {
    const view = render(transcript(0));
    const figure = () => view.container.querySelector<HTMLElement>('[role="figure"]')!;
    await waitFor(() => expect(figure().querySelector(".mark-rect path")).not.toBeNull());
    const drawn = figure().querySelector("svg");

    for (let update = 1; update <= 12; update += 1) {
      view.rerender(transcript(update));
      // A redraw would first empty the view and say it is loading again.
      expect(figure().getAttribute("aria-busy")).toBe("false");
      expect(figure().querySelector("svg")).toBe(drawn);
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(figure().querySelector("svg")).toBe(drawn);
    expect(figure().querySelectorAll(".mark-rect path")).toHaveLength(2);
  });
});
