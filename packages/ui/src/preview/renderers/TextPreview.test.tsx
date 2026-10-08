import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PreviewProps } from "../types";
import { TEXT_VIEW_SETTINGS, TextPreview } from "./TextPreview";
import {
  manualGate,
  numberedWindows,
  placeScroller,
  WindowedHost,
  type Gate,
} from "./windowedHost.testkit";

afterEach(cleanup);

function props(over: Partial<PreviewProps> = {}): PreviewProps {
  return {
    facts: { mime: "text/plain", name: "notes.txt", size: 120 },
    content: { kind: "text", text: "first line\nsecond line" },
    version: "etag-1",
    status: "ready",
    onDownload: () => {},
    ...over,
  };
}

function body(): HTMLElement {
  return screen.getByTestId("preview-text-body");
}

describe("a text preview", () => {
  it("shows the file's lines as they are", () => {
    render(<TextPreview {...props()} />);

    expect(body().tagName).toBe("PRE");
    expect(body().textContent).toBe("first line\nsecond line");
  });

  it("shows markup as characters, never as elements", () => {
    // A plain-text file is text. Interpreting it would run whatever an agent, or
    // whoever handed the agent the file, wrote into it.
    const { container } = render(
      <TextPreview
        {...props({
          content: { kind: "text", text: "<img src=x onerror=alert(1)><b>bold</b>" },
        })}
      />,
    );

    expect(body().textContent).toBe("<img src=x onerror=alert(1)><b>bold</b>");
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("wraps long lines on request and remembers the choice for the host", () => {
    const onViewState = vi.fn();
    render(<TextPreview {...props({ onViewState })} />);

    const toggle = screen.getByRole("button", { name: /soft wrap/i });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(body().dataset.wrap).toBe("off");

    fireEvent.click(toggle);

    expect(screen.getByRole("button", { name: /soft wrap/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(body().dataset.wrap).toBe("on");
    expect(onViewState).toHaveBeenCalledWith({ wrap: true });
  });

  it("opens wrapped where the host last left it wrapped", () => {
    render(<TextPreview {...props({ viewState: { wrap: true } })} />);

    expect(body().dataset.wrap).toBe("on");
    expect(screen.getByRole("button", { name: /soft wrap/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("ignores view state it did not write", () => {
    render(<TextPreview {...props({ viewState: "wrap" })} />);

    expect(body().dataset.wrap).toBe("off");
  });

  it("keeps the wrap across a rewrite of the file", () => {
    const { rerender } = render(<TextPreview {...props()} />);
    fireEvent.click(screen.getByRole("button", { name: /soft wrap/i }));

    rerender(
      <TextPreview {...props({ version: "etag-2", content: { kind: "text", text: "new" } })} />,
    );

    expect(body().dataset.wrap).toBe("on");
    expect(body().textContent).toBe("new");
  });

  it("says so when there are no bytes", () => {
    render(<TextPreview {...props({ content: { kind: "none" } })} />);

    expect(screen.queryByTestId("preview-text-body")).toBeNull();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });
});

// A surface with no room for a second bar takes the settings into chrome of its
// own. Then the renderer must draw none, and — the part that actually bites —
// must read the value from the host on every render: a renderer still holding
// its own copy would answer the file with one wrap while the control the person
// just pressed shows the other.
describe("a text preview whose host owns the controls", () => {
  it("declares the setting it offers, so a host can draw it without knowing the renderer", () => {
    expect(TEXT_VIEW_SETTINGS.map((setting) => setting.key)).toEqual(["wrap"]);
    expect(TEXT_VIEW_SETTINGS[0]?.label).toBe("Soft wrap");
  });

  it("draws no bar of its own", () => {
    const { container } = render(<TextPreview {...props({ viewControls: "host" })} />);

    expect(screen.queryByRole("button", { name: /soft wrap/i })).toBeNull();
    expect(container.querySelector(".alk-preview-text__bar")).toBeNull();
    expect(body().dataset.wrap).toBe("off");
  });

  it("draws whatever the host is holding, on every render", () => {
    const { rerender } = render(<TextPreview {...props({ viewControls: "host" })} />);
    expect(body().dataset.wrap).toBe("off");

    rerender(<TextPreview {...props({ viewControls: "host", viewState: { wrap: true } })} />);
    expect(body().dataset.wrap).toBe("on");

    rerender(<TextPreview {...props({ viewControls: "host", viewState: { wrap: false } })} />);
    expect(body().dataset.wrap).toBe("off");
  });

  it("keeps the renderer's own bar wherever the host has not asked for it", () => {
    render(<TextPreview {...props()} />);

    expect(screen.getByRole("button", { name: /soft wrap/i })).toBeInTheDocument();
  });
});

describe("a text file that has not all arrived", () => {
  const TOTAL = 13_002_342; // 12.4 MB
  const windows = numberedWindows(3, 4);

  function host(gate?: Gate, over: Partial<PreviewProps> = {}) {
    return render(
      <WindowedHost
        windows={windows}
        total={TOTAL}
        gate={gate}
        draw={(content) => <TextPreview {...props({ content, ...over })} />}
      />,
    );
  }

  it("says how much of the file is here and offers the rest", () => {
    host();

    expect(screen.getByRole("status")).toHaveTextContent("Showing 1 MB of 13 MB");
    expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
    expect(body().textContent).toBe(windows[0]);
  });

  it("brings the next window on the button, saying so while it is on its way", async () => {
    const gate = manualGate();
    host(gate);

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));

    expect(screen.getByRole("status")).toHaveTextContent("Loading more…");
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
    await act(async () => gate.open());
    expect(body().textContent).toBe(windows[0] + windows[1]);
    expect(screen.getByRole("status")).toHaveTextContent("Showing 2.1 MB of 13 MB");
  });

  it("asks for the next window once when a scroll comes within two screens of the end", async () => {
    const gate = manualGate();
    host(gate);
    const scroller = body();

    placeScroller(scroller, { scrollHeight: 5000, clientHeight: 500, top: 3500 });
    fireEvent.scroll(scroller);
    // Still near the end while the window is on its way: no second ask.
    fireEvent.scroll(scroller);
    fireEvent.scroll(scroller);
    await act(async () => gate.open());
    // And again after it landed, at the same place — a new window, asked once.
    expect(body().textContent).toBe(windows[0] + windows[1]);
  });

  it("does not ask while the reader is more than two screens from the end", async () => {
    host();
    const scroller = body();

    placeScroller(scroller, { scrollHeight: 5000, clientHeight: 500, top: 3499 });
    fireEvent.scroll(scroller);
    await act(async () => {});

    expect(body().textContent).toBe(windows[0]);
  });

  it("asks again from a scroll once the window it asked for has landed", async () => {
    host();
    const scroller = body();
    placeScroller(scroller, { scrollHeight: 5000, clientHeight: 500, top: 4000 });

    fireEvent.scroll(scroller);
    await act(async () => {});
    fireEvent.scroll(scroller);
    await act(async () => {});

    expect(body().textContent).toBe(windows.join(""));
    // The whole file is here: the line is gone.
    expect(screen.queryByTestId("preview-more")).toBeNull();
  });

  it("keeps the reader's place and soft wrap as a window lands", async () => {
    const gate = manualGate();
    host(gate);
    fireEvent.click(screen.getByRole("button", { name: /soft wrap/i }));
    const scroller = body();
    placeScroller(scroller, { scrollHeight: 5000, clientHeight: 500, top: 1234 });

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    await act(async () => gate.open());

    expect(body()).toBe(scroller);
    expect(body().scrollTop).toBe(1234);
    expect(body().dataset.wrap).toBe("on");
    expect(body().textContent).toBe(windows[0] + windows[1]);
  });

  it("says a window failed and asks again only from the button", async () => {
    const gate = manualGate();
    host(gate);
    const scroller = body();
    placeScroller(scroller, { scrollHeight: 5000, clientHeight: 500, top: 4000 });

    fireEvent.scroll(scroller);
    await act(async () => gate.fail());

    expect(screen.getByRole("status")).toHaveTextContent(
      "The next part of the file could not be loaded.",
    );
    fireEvent.scroll(scroller);
    expect(screen.queryByText("Loading more…")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    await act(async () => gate.open());
    expect(body().textContent).toBe(windows[0] + windows[1]);
  });

  it("draws no line for a file that arrived whole", () => {
    render(<TextPreview {...props({ content: { kind: "text", text: "all\n" } })} />);
    expect(screen.queryByTestId("preview-more")).toBeNull();

    cleanup();
    render(
      <TextPreview
        {...props({
          content: { kind: "text", text: "all\n", loaded: 4, total: 4, more: async () => {} },
        })}
      />,
    );
    expect(screen.queryByTestId("preview-more")).toBeNull();
  });
});
