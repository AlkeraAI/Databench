import { render, screen } from "@testing-library/react";

import type { CellOutput, MimeBundle } from "../model/types";
import { registerNotebookRenderers } from "../register";
import { registerDefaultOutputRenderers } from "./defaults";
import { MAX_BUNDLE_DEPTH, OutputArea, OutputView, mergeStreams } from "./OutputView";
import { OutputRegistry } from "./registry";
import type { OutputAreaContext, OutputRendererProps } from "./types";

const base: OutputAreaContext = { theme: "light", readonly: false, cellId: "c1" };

function defaults(): OutputRegistry {
  const registry = new OutputRegistry();
  registerDefaultOutputRenderers(registry);
  return registry;
}

describe("OutputArea", () => {
  it("draws display, stream and error outputs in order", () => {
    const outputs: CellOutput[] = [
      { output_id: "a", type: "stream", name: "stdout", text: "hello\n" },
      { output_id: "b", type: "display", data: { "text/plain": "42" } },
      { output_id: "c", type: "error", error: { ename: "ValueError", evalue: "bad", traceback: ["line 1", "line 2"] } },
    ];
    const { container } = render(<OutputArea outputs={outputs} context={base} registry={defaults()} />);
    const ids = [...container.querySelectorAll("[data-output-id]")].map((el) => el.getAttribute("data-output-id"));
    expect(ids).toEqual(["a", "b", "c"]);
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("ValueError: bad");
    expect(container.querySelector(".nb-output-error-traceback")?.textContent).toBe("line 1\nline 2");
  });

  it("joins adjacent chunks of one stream, but not across streams", () => {
    const outputs: CellOutput[] = [
      { output_id: "1", type: "stream", name: "stdout", text: "a" },
      { output_id: "2", type: "stream", name: "stdout", text: "b" },
      { output_id: "3", type: "stream", name: "stderr", text: "c" },
      { output_id: "4", type: "stream", name: "stdout", text: "d" },
    ];
    const merged = mergeStreams(outputs);
    expect(merged.map((o) => (o.type === "stream" ? [o.name, o.text] : null))).toEqual([
      ["stdout", "ab"],
      ["stderr", "c"],
      ["stdout", "d"],
    ]);
  });

  it("collapses a progress bar's carriage returns across chunks", () => {
    const outputs: CellOutput[] = [
      { output_id: "1", type: "stream", name: "stderr", text: "10%\r" },
      { output_id: "2", type: "stream", name: "stderr", text: "100%\n" },
    ];
    const { container } = render(<OutputArea outputs={outputs} context={base} registry={defaults()} />);
    const stream = container.querySelector("[data-stream='stderr']");
    expect(stream).toHaveTextContent("100%");
    expect(stream?.textContent).not.toContain("10%1");
    expect(stream).toHaveClass("nb-output-stream--stderr");
  });

  it("draws nothing for no outputs", () => {
    const { container } = render(<OutputArea outputs={[]} context={base} registry={defaults()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("carries the theme to the output for themed tokens", () => {
    const { container } = render(
      <OutputArea outputs={[{ output_id: "a", type: "stream", name: "stdout", text: "x" }]} context={{ ...base, theme: "dark" }} />,
    );
    expect(container.querySelector(".nb-output")).toHaveAttribute("data-nb-theme", "dark");
  });
});

describe("renderer failures", () => {
  it("a renderer that throws falls back to the bundle's text without breaking its neighbours", () => {
    const registry = defaults();
    const Broken = (): never => {
      throw new Error("boom");
    };
    registry.register({ id: "broken", mimes: ["application/x-broken"], rank: 0, place: "app", Component: Broken });
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <OutputArea
        outputs={[
          { output_id: "a", type: "display", data: { "application/x-broken": 1, "text/plain": "plain repr" } },
          { output_id: "b", type: "display", data: { "text/plain": "neighbour" } },
        ]}
        context={base}
        registry={registry}
      />,
    );
    spy.mockRestore();
    expect(screen.getByText("This output could not be drawn.")).toBeInTheDocument();
    expect(screen.getByText("plain repr")).toBeInTheDocument();
    expect(screen.getByText("neighbour")).toBeInTheDocument();
  });
});

describe("nested bundles", () => {
  it("renderBundle draws children through the same registry with derived output ids", () => {
    const registry = new OutputRegistry();
    const seen: string[] = [];
    const Nest = ({ data, context }: OutputRendererProps) => {
      seen.push(context.outputId);
      const children = data as MimeBundle[];
      return <div>{children.map((child, i) => context.renderBundle(child, `k${i}`))}</div>;
    };
    const Leaf = ({ data, context }: OutputRendererProps) => {
      seen.push(context.outputId);
      return <span>{String(data)}</span>;
    };
    registry.register({ id: "nest", mimes: ["application/x-nest"], rank: 0, place: "app", Component: Nest });
    registry.register({ id: "leaf", mimes: ["text/plain"], rank: 0, place: "app", Component: Leaf });
    render(
      <OutputView
        output={{ output_id: "o", type: "display", data: { "application/x-nest": [{ "text/plain": "one" }, { "text/plain": "two" }] } }}
        context={base}
        registry={registry}
      />,
    );
    expect(screen.getByText("one")).toBeInTheDocument();
    expect(screen.getByText("two")).toBeInTheDocument();
    expect(seen).toEqual(["o", "o/k0", "o/k1"]);
  });

  it("stops at the nesting limit instead of recursing without end", () => {
    const registry = defaults();
    let bundle: MimeBundle = { "text/plain": "deepest" };
    for (let i = 0; i < MAX_BUNDLE_DEPTH + 2; i += 1) {
      bundle = { "application/vnd.alkera.layout+json": { type: "vstack", items: [bundle] } };
    }
    render(<OutputView output={{ output_id: "o", type: "display", data: bundle }} context={base} registry={registry} />);
    expect(screen.getByText("Nested too deeply to draw.")).toBeInTheDocument();
    expect(screen.queryByText("deepest")).not.toBeInTheDocument();
  });
});

describe("Markdown cells", () => {
  // `alkera.md` sends Markdown with an HTML copy beside it. The HTML copy
  // knows nothing of math, so the Markdown is what draws, in the app, even
  // when a content frame could draw the HTML.
  const bundle: MimeBundle = {
    "text/html": "<p>Euler: $e^{i\\pi} + 1 = 0$</p>",
    "text/markdown": "Euler: $e^{i\\pi} + 1 = 0$ and\n\n$$\\sum_{k=1}^n k$$",
    "text/plain": "Euler",
  };
  const frame = { bootstrapUrl: "https://content.example/c/nb-output/abc", loadModule: () => Promise.resolve("") };

  it.each([
    ["without a frame", base],
    ["with a frame", { ...base, frame }],
  ])("draw inline and display math with KaTeX %s", (_, context) => {
    const registry = new OutputRegistry();
    registerNotebookRenderers(registry);
    const { container } = render(
      <OutputArea outputs={[{ output_id: "m", type: "display", data: bundle }]} context={context} registry={registry} />,
    );
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector(".nb-output-markdown p .nb-md-math .katex")).not.toBeNull();
    expect(container.querySelector(".nb-md-math--display .katex-display")).not.toBeNull();
    expect(container.textContent).not.toContain("$e^");
  });
});

describe("an output stored beside the notebook", () => {
  const SHA = "a".repeat(64);
  const ref = (bytes: number) => ({ "application/vnd.alkera.ref+json": { sha256: SHA, mime: "image/png", bytes } });
  const display = (bundle: MimeBundle): CellOutput => ({ output_id: "o1", type: "display", data: bundle }) as CellOutput;

  it("shows a stored image from its stored copy", () => {
    const { container } = render(
      <OutputView output={display({ "image/png": ref(7_654_321), "text/plain": "<Figure>" })} context={{ ...base, blobUrl: (sha) => `/blobs/${sha}` }} registry={defaults()} />,
    );
    const img = container.querySelector("img")!;
    expect(img.getAttribute("src")).toBe(`/blobs/${SHA}`);
    expect(container.textContent).not.toContain("<Figure>");
  });

  it("names anything else with its size and a way to open it", () => {
    render(<OutputView output={display({ "text/html": { "application/vnd.alkera.ref+json": { sha256: SHA, mime: "text/html", bytes: 3_145_728 } }, "text/plain": "x" })} context={{ ...base, blobUrl: (sha) => `/blobs/${sha}` }} registry={defaults()} />);
    expect(screen.getByText(/This output is 3\.0 MB\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open it" })).toHaveAttribute("href", `/blobs/${SHA}`);
  });

  it("says it is too large where the host cannot serve stored outputs", () => {
    render(<OutputView output={display({ "image/png": ref(7_654_321) })} context={base} registry={defaults()} />);
    expect(screen.getByText("Output too large to show here (7.3 MB).")).toBeInTheDocument();
  });
});
