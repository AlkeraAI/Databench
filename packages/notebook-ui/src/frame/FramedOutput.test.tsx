import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FrameServices, OutputContext, OutputTheme } from "../outputs/types";
import { FRAME_LOAD_TIMEOUT_MS, FramedOutput, frameRenderers } from "./FramedOutput";
import { MAX_FRAME_HEIGHT } from "./protocol";

const BOOTSTRAP = "https://content.example/c/nb-output/abcdef12";

function services(overrides: Partial<FrameServices> = {}): FrameServices {
  return {
    bootstrapUrl: BOOTSTRAP,
    loadModule: async (name) => `/* ${name} */`,
    ...overrides,
  };
}

function context(frame: FrameServices | undefined, theme: OutputTheme = "light"): OutputContext {
  return {
    theme,
    readonly: false,
    cellId: "c1",
    outputId: "o1",
    ...(frame ? { frame } : {}),
    renderBundle: () => null,
  };
}

function frameElement(): HTMLIFrameElement {
  const iframe = document.querySelector("iframe");
  if (!iframe) throw new Error("no frame rendered");
  return iframe;
}

function nonceOf(iframe: HTMLIFrameElement): string {
  return new URL(iframe.src).hash.replace("#n=", "");
}

/** What the component posted into the frame's window. */
function received(iframe: HTMLIFrameElement): Array<Record<string, unknown>> {
  const win = iframe.contentWindow as Window & { __got?: Array<Record<string, unknown>> };
  if (!win.__got) {
    const got: Array<Record<string, unknown>> = [];
    win.__got = got;
    win.addEventListener("message", (event) => got.push(event.data as Record<string, unknown>));
  }
  return win.__got;
}

/** Sends a message as the frame's bootstrap would. */
async function fromFrame(iframe: HTMLIFrameElement, data: Record<string, unknown>, nonce = nonceOf(iframe)): Promise<void> {
  await act(async () => {
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { alk: 1, frame: nonce, ...data },
        origin: "null",
        source: iframe.contentWindow,
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

const settle = () => act(async () => new Promise((resolve) => setTimeout(resolve, 10)));

afterEach(() => {
  vi.useRealTimers();
});

describe("FramedOutput", () => {
  it("mounts a scripts-only sandboxed frame on the bootstrap page with a fresh nonce", () => {
    render(<FramedOutput mime="text/html" data="<b>x</b>" bundle={{}} context={context(services())} />);
    const iframe = frameElement();
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts");
    expect(iframe.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(iframe.src.startsWith(`${BOOTSTRAP}#n=`)).toBe(true);
    expect(nonceOf(iframe)).toMatch(/^[0-9a-f]{32}$/);
  });

  it("answers ready with the renderer module and the output, and follows the theme", async () => {
    const frame = services();
    const view = render(<FramedOutput mime="text/html" data="<b>x</b>" bundle={{}} context={context(frame)} />);
    const iframe = frameElement();
    const got = received(iframe);
    await fromFrame(iframe, { type: "ready" });
    await settle();
    expect(got.map((m) => m.type)).toEqual(["module", "init"]);
    expect(got[0]).toMatchObject({ name: "nb-html", code: "/* nb-html */", frame: nonceOf(iframe) });
    expect(got[1]).toMatchObject({ mime: "text/html", data: "<b>x</b>", output_id: "o1", theme: "light", readonly: false });

    view.rerender(<FramedOutput mime="text/html" data="<b>x</b>" bundle={{}} context={context(frame, "dark")} />);
    await settle();
    expect(got.at(-1)).toMatchObject({ type: "theme", theme: "dark" });
  });

  it("takes the height the frame reports, clamped", async () => {
    render(<FramedOutput mime="text/html" data="x" bundle={{}} context={context(services())} />);
    const iframe = frameElement();
    await fromFrame(iframe, { type: "size", height: 321 });
    expect(iframe.style.height).toBe("321px");
    await fromFrame(iframe, { type: "size", height: 1e9 });
    expect(iframe.style.height).toBe(`${MAX_FRAME_HEIGHT}px`);
  });

  it("ignores a message carrying another frame's nonce", async () => {
    render(<FramedOutput mime="text/html" data="x" bundle={{}} context={context(services())} />);
    const iframe = frameElement();
    await fromFrame(iframe, { type: "size", height: 321 }, "f".repeat(32));
    expect(iframe.style.height).toBe("");
  });

  it("shows the frame's errors", async () => {
    render(<FramedOutput mime="text/html" data="x" bundle={{}} context={context(services())} />);
    await fromFrame(frameElement(), { type: "error", message: "ReferenceError: x is not defined" });
    expect(screen.getByRole("alert").textContent).toBe("ReferenceError: x is not defined");
  });

  it("asks before following a link and does not follow it itself", async () => {
    const asked: string[] = [];
    render(<FramedOutput mime="text/html" data="x" bundle={{}} context={context(services({ confirmLink: (href) => asked.push(href) }))} />);
    await fromFrame(frameElement(), { type: "link", href: "https://example.com/" });
    expect(asked).toEqual(["https://example.com/"]);
  });

  it("reports a frame that never answers, and tries again in a new frame", async () => {
    vi.useFakeTimers();
    render(<FramedOutput mime="text/html" data="x" bundle={{}} context={context(services())} />);
    const first = nonceOf(frameElement());
    act(() => {
      vi.advanceTimersByTime(FRAME_LOAD_TIMEOUT_MS + 1);
    });
    expect(screen.getByText("The content server didn’t answer, so this output couldn’t be loaded.")).toBeTruthy();
    expect(document.querySelector("iframe")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    const second = nonceOf(frameElement());
    expect(second).not.toBe(first);
  });

  it("does not call a frame that answered unreachable", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<FramedOutput mime="text/html" data="x" bundle={{}} context={context(services())} />);
    await fromFrame(frameElement(), { type: "ready" });
    act(() => {
      vi.advanceTimersByTime(FRAME_LOAD_TIMEOUT_MS + 1);
    });
    expect(document.querySelector("iframe")).not.toBeNull();
  });

  it("stops a frame whose document navigated away, and reloads it on request", async () => {
    render(<FramedOutput mime="text/html" data="x" bundle={{}} context={context(services())} />);
    const iframe = frameElement();
    const first = nonceOf(iframe);
    fireEvent.load(iframe);
    fireEvent.load(iframe);
    expect(screen.getByText("This output tried to leave its frame, so it was stopped.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Reload output" }));
    expect(nonceOf(frameElement())).not.toBe(first);
  });

  it("says so when the host serves no frame", () => {
    render(<FramedOutput mime="text/html" data="x" bundle={{}} context={context(undefined)} />);
    expect(screen.getByText("This output can’t be shown here.")).toBeTruthy();
    expect(document.querySelector("iframe")).toBeNull();
  });
});

describe("frameRenderers", () => {
  it("cover the framed MIME types and are available only with frame services", () => {
    const mimes = frameRenderers.flatMap((r) => r.mimes).sort();
    expect(mimes).toEqual(
      [
        "application/vnd.jupyter.widget-view+json",
        "application/vnd.plotly.v1+json",
        "application/vnd.vegalite.v5+json",
        "application/vnd.vegalite.v6+json",
        "image/svg+xml",
        "text/html",
      ].sort(),
    );
    for (const renderer of frameRenderers) {
      expect(renderer.place).toBe("frame");
      expect(renderer.available?.(context(undefined))).toBe(false);
      expect(renderer.available?.(context(services()))).toBe(true);
    }
  });
});
