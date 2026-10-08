// Inline uploads in the shared composer. A pasted, dropped or picked file
// becomes a pill in the field — `[Image 1]`, `[File 1]`, numbered per kind —
// and is uploaded at once through the host's uploader. Pinned here: the pill
// and its numbering, removal renumbering the rest, Send held while an upload
// is in flight and released after, the outgoing text carrying the markdown
// for each landed path, size refusals with a stated reason, retries against a
// transient failure, and a persistent failure withdrawing the pill, telling
// the reader why, and leaving the message sendable without it.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatFilesProvider } from "../../primitives/render/Markdown/ChatFiles";
import { OrderTicket } from "../transcript/OrderTicket";
import { Composer } from "./Composer";
import { describeUploadLimit, type ComposerUploader } from "./uploads";

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

function png(name = "shot.png"): File {
  return new File(["\x89PNG"], name, { type: "image/png" });
}
function csv(name = "q.csv"): File {
  return new File(["a,b"], name, { type: "text/csv" });
}

/** An uploader whose answers the test scripts: a deferred promise per call. */
function scriptedUploader(maxBytes?: number) {
  const calls: Array<{ file: File; kind: string; n: number; resolve: (path: string) => void; reject: (e: Error) => void }> = [];
  const uploader: ComposerUploader = {
    maxBytes,
    upload: (file, hint) =>
      new Promise<{ path: string }>((resolve, reject) => {
        calls.push({ file, kind: hint.kind, n: hint.n, resolve: (path) => resolve({ path }), reject });
      }),
    retryDelayMs: () => 0,
  };
  return { uploader, calls };
}

function field(): HTMLTextAreaElement {
  return screen.getByRole("textbox", { name: "Message Databench" }) as HTMLTextAreaElement;
}

function paste(files: File[]): void {
  fireEvent.paste(field(), { clipboardData: { files, getData: () => "" } });
}

describe("a refused file's line", () => {
  // The refusal must not outlive the batch it was about.
  it("goes when the reader takes the batch's files off", async () => {
    const { uploader } = scriptedUploader();
    render(<Composer {...base()} uploader={uploader} />);
    paste([png("a.png"), new File(["x"], "big.bin")]);
    expect(screen.getByText(/big\.bin can't be attached/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove a.png (Image 1)" }));
    expect(screen.queryByText(/big\.bin can't be attached/)).toBeNull();
  });

  it("leaves a line about something else where it is", async () => {
    const { uploader, calls } = scriptedUploader();
    render(<Composer {...base()} uploader={uploader} />);
    paste([png("a.png"), png("b.png")]);
    // Every attempt at a.png refused, so the pill is withdrawn with a reason.
    for (let i = 0; i < 6; i += 1) {
      const pending = calls.filter((c) => c.file.name === "a.png").at(-1);
      await act(async () => pending?.reject(new Error("no room")));
    }
    await waitFor(() => expect(screen.getByText(/a\.png could not be uploaded/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Remove b.png (Image 1)" }));
    expect(screen.getByText(/a\.png could not be uploaded/)).toBeTruthy();
  });
});

describe("inline uploads", () => {
  it("does nothing on paste where the host has no uploader", () => {
    render(<Composer {...base()} />);
    paste([png()]);
    expect(field().value).toBe("");
    expect(screen.queryByRole("list", { name: "Uploads" })).toBeNull();
  });

  it("a pasted image becomes [Image 1] at the caret and an upload starts at once", async () => {
    const { uploader, calls } = scriptedUploader();
    render(<Composer {...base()} uploader={uploader} />);
    fireEvent.change(field(), { target: { value: "see " } });
    field().setSelectionRange(4, 4);
    paste([png()]);
    expect(field().value).toBe("see [Image 1] ");
    expect(calls).toHaveLength(1);
    expect(calls[0].kind).toBe("image");
    expect(calls[0].n).toBe(1);
    expect(calls[0].file.name).toBe("shot.png");
    const pill = screen.getByRole("list", { name: "Uploads" }).querySelector("li")!;
    expect(pill.getAttribute("data-state")).toBe("uploading");
    expect(screen.getByRole("progressbar", { name: "Uploading shot.png" })).toBeTruthy();
    // Send is held with a reason while the bytes are in flight.
    expect(screen.getByRole("button", { name: /Send: Waiting for uploads/ }).hasAttribute("disabled")).toBe(true);
    await act(async () => calls[0].resolve("paste-1-ab12.png"));
    await waitFor(() => expect(pill.getAttribute("data-state")).toBe("uploaded"));
    expect(screen.getByRole("img", { name: "Uploaded" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Send" }).hasAttribute("disabled")).toBe(false);
  });

  it("images and files number separately; a dropped file is a [File n] pill", () => {
    const { uploader, calls } = scriptedUploader();
    render(<Composer {...base()} uploader={uploader} />);
    paste([png("a.png"), csv("q.csv"), png("b.png")]);
    expect(field().value).toBe("[Image 1] [File 1] [Image 2] ");
    expect(calls.map((c) => `${c.kind}${c.n}`)).toEqual(["image1", "file1", "image2"]);
    const wrap = field().parentElement!;
    fireEvent.drop(wrap, { dataTransfer: { files: [csv("r.csv")] } });
    expect(field().value).toBe("[Image 1] [File 1] [Image 2] [File 2] ");
    expect(calls[3]).toMatchObject({ kind: "file", n: 2 });
  });

  it("removing a pill renumbers the ones after it, in the text and on the pills", async () => {
    const { uploader, calls } = scriptedUploader();
    render(<Composer {...base()} uploader={uploader} />);
    paste([png("a.png"), png("b.png"), png("c.png")]);
    await act(async () => calls.forEach((c, i) => c.resolve(`paste-${i + 1}-x.png`)));
    fireEvent.click(screen.getByRole("button", { name: "Remove b.png (Image 2)" }));
    expect(field().value).toBe("[Image 1] [Image 2] ");
    const tokens = Array.from(document.querySelectorAll(".chat-composer-upload__token")).map((n) => n.textContent);
    expect(tokens).toEqual(["[Image 1]", "[Image 2]"]);
    // The pill that was [Image 3] is now [Image 2] and still carries c.png.
    const pills = screen.getByRole("list", { name: "Uploads" }).querySelectorAll("li");
    expect(pills[1].getAttribute("title")).toContain("c.png");
  });

  it("backspace right after a pill takes the whole pill and its upload", () => {
    const { uploader } = scriptedUploader();
    render(<Composer {...base()} uploader={uploader} />);
    paste([png("a.png")]);
    const el = field();
    fireEvent.change(el, { target: { value: "[Image 1]" } });
    el.setSelectionRange(9, 9);
    fireEvent.keyDown(el, { key: "Backspace" });
    expect(el.value).toBe("");
    expect(screen.queryByRole("list", { name: "Uploads" })).toBeNull();
  });

  it("send replaces each pill with the transcript markdown for its landed path", async () => {
    const { uploader, calls } = scriptedUploader();
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} />);
    fireEvent.change(field(), { target: { value: "compare " } });
    field().setSelectionRange(8, 8);
    paste([png("shot.png"), csv("numbers [q3].csv")]);
    fireEvent.change(field(), { target: { value: `${field().value}please` } });
    await act(async () => {
      calls[0].resolve("paste-1-ab12.png");
      calls[1].resolve("file-1-cd34.csv");
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" }).hasAttribute("disabled")).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSend).toHaveBeenCalledWith(
      "compare ![Image 1](paste-1-ab12.png) [File 1: numbers q3.csv](file-1-cd34.csv) please",
    );
    expect(field().value).toBe("");
    expect(screen.queryByRole("list", { name: "Uploads" })).toBeNull();
  });

  it("Enter does not send while an upload is in flight, and does once it lands", async () => {
    const { uploader, calls } = scriptedUploader();
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} />);
    paste([png()]);
    fireEvent.keyDown(field(), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    await act(async () => calls[0].resolve("paste-1-x.png"));
    fireEvent.keyDown(field(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("![Image 1](paste-1-x.png)");
  });

  it("a file over a cap the uploader declares is refused in the limit's own words, and gets no pill", () => {
    const { uploader, calls } = scriptedUploader(10 * 1024 * 1024);
    render(<Composer {...base()} uploader={uploader} />);
    const big = new File([new Uint8Array(10 * 1024 * 1024 + 1)], "huge.bin");
    paste([big, csv("ok.csv")]);
    expect(screen.getByRole("alert").textContent).toBe(
      "huge.bin exceeded the maximum upload size of 10 MB.",
    );
    expect(field().value).toBe("[File 1] ");
    expect(calls).toHaveLength(1);
    expect(calls[0].file.name).toBe("ok.csv");
  });

  it("an uploader that declares no cap refuses nothing on SIZE — the transport's ceiling is the only one", () => {
    const { uploader, calls } = scriptedUploader();
    render(<Composer {...base()} uploader={uploader} />);
    // A kind a message takes, at a size no local rule bounds.
    paste([new File([new Uint8Array(4 * 1024 * 1024)], "big.csv")]);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(calls).toHaveLength(1);
    expect(calls[0].file.name).toBe("big.csv");
  });

  // The figure is FLOORED, never rounded: the number the reader is shown has to
  // be a size the server actually accepts. Rounding 10,485,760 bytes up to
  // "10.5 MB" named a size that is in fact refused, and disagreed with the
  // daemon — which floors the same constant — about a single ceiling.
  it.each([
    [999, "999 B"],
    [10 * 1024 * 1024, "10 MB"],
    [1_999_999, "1 MB"],
    [1_000_000_000_000, "1000 GB"],
  ])("a ceiling of %i bytes reads as %s, the way the server states it", (bytes, said) => {
    expect(describeUploadLimit(bytes)).toBe(said);
  });

  it("never names a figure the bound itself would refuse", () => {
    for (const bytes of [1500, 10 * 1024 * 1024, 5_500_000, 999_999_999]) {
      const [figure, unit] = describeUploadLimit(bytes).split(" ");
      const scale = 1000 ** ["B", "KB", "MB", "GB"].indexOf(unit);
      expect(Number(figure) * scale).toBeLessThanOrEqual(bytes);
    }
  });

  it("a transient failure is retried with the same File and lands on the second try", async () => {
    const { uploader, calls } = scriptedUploader();
    const onUploadFailed = vi.fn();
    render(<Composer {...base()} uploader={uploader} onUploadFailed={onUploadFailed} />);
    paste([png("a.png")]);
    await act(async () => calls[0].reject(new Error("503 from the store")));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].file).toBe(calls[0].file);
    const pill = screen.getByRole("list", { name: "Uploads" }).querySelector("li")!;
    await waitFor(() => expect(pill.getAttribute("data-state")).toBe("failed"));
    expect(pill.getAttribute("title")).toContain("503 from the store");
    await act(async () => calls[1].resolve("paste-1-x.png"));
    await waitFor(() => expect(pill.getAttribute("data-state")).toBe("uploaded"));
    expect(field().value).toBe("[Image 1] ");
    expect(onUploadFailed).not.toHaveBeenCalled();
  });

  it("a persistent failure withdraws the pill after the last attempt, says why, and the rest still sends", async () => {
    const { uploader, calls } = scriptedUploader();
    const onUploadFailed = vi.fn();
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} onUploadFailed={onUploadFailed} />);
    fireEvent.change(field(), { target: { value: "hi " } });
    field().setSelectionRange(3, 3);
    paste([png("a.png"), csv("q.csv")]);
    await act(async () => calls[1].resolve("file-1-x.csv"));
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await waitFor(() => expect(calls.filter((c) => c.kind === "image")).toHaveLength(attempt + 1));
      const call = calls.filter((c) => c.kind === "image")[attempt];
      await act(async () => call.reject(new Error("quota exceeded")));
    }
    await waitFor(() => expect(onUploadFailed).toHaveBeenCalledWith("a.png", "quota exceeded"));
    expect(calls.filter((c) => c.kind === "image")).toHaveLength(3);
    expect(field().value).toBe("hi [File 1] ");
    expect(screen.getByRole("alert").textContent).toBe("a.png could not be uploaded: quota exceeded");
    expect(screen.getByRole("list", { name: "Uploads" }).querySelectorAll("li")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSend).toHaveBeenCalledWith("hi [File 1: q.csv](file-1-x.csv)");
  });

  it("the attach door stages through the uploader when one is present", () => {
    const { uploader, calls } = scriptedUploader();
    render(<Composer {...base()} uploader={uploader} />);
    const input = document.querySelector<HTMLInputElement>(".chat-composer-attachinput")!;
    fireEvent.change(input, { target: { files: [csv("picked.csv")] } });
    expect(field().value).toBe("[File 1] ");
    expect(calls[0].file.name).toBe("picked.csv");
  });
});

describe("a pill whose token left the text", () => {
  // The pills stay on screen with their check marks whatever the reader does to
  // the words, so a pill on screen is a file on the message. Its token only
  // says where in the words it sits; without one it rides after them.
  async function twoLanded(onSend: (text: string) => void): Promise<void> {
    const { uploader, calls } = scriptedUploader();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} />);
    paste([png("blue-circle.png"), png("green-yellow.jpg")]);
    expect(field().value).toBe("[Image 1] [Image 2] ");
    await act(async () => {
      calls[0].resolve("uploads/paste-1-yynv.png");
      calls[1].resolve("uploads/paste-2-z5cr.jpg");
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" }).hasAttribute("disabled")).toBe(false));
  }

  it("still carries both images when the reader typed over every token", async () => {
    const onSend = vi.fn();
    await twoLanded(onSend);
    fireEvent.change(field(), { target: { value: "describe these images" } });
    expect(screen.getByRole("list", { name: "Uploads" }).querySelectorAll("li")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const sent = "describe these images\n\n![Image 1](uploads/paste-1-yynv.png)\n![Image 2](uploads/paste-2-z5cr.jpg)";
    expect(onSend).toHaveBeenCalledWith(sent);

    // And the reader's bubble shows both, the way it shows an inline pill.
    cleanup();
    render(
      <ChatFilesProvider resolver={{ resolveUrl: vi.fn(async (path: string) => `blob:${path}`), openPath: vi.fn() }}>
        <OrderTicket text={sent} at="now" />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByRole("img", { name: "Image 1" }).tagName).toBe("IMG"));
    expect(screen.getByRole("img", { name: "Image 2" }).tagName).toBe("IMG");
    expect(screen.getByText("describe these images")).toBeTruthy();
  });

  it("keeps a token that stayed where the reader put it, and adds the one that left after the words", async () => {
    const onSend = vi.fn();
    await twoLanded(onSend);
    fireEvent.change(field(), { target: { value: "compare [Image 2] with the first" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSend).toHaveBeenCalledWith(
      "compare ![Image 2](uploads/paste-2-z5cr.jpg) with the first\n\n![Image 1](uploads/paste-1-yynv.png)",
    );
  });

  it("sends the images alone when the reader cleared the field", async () => {
    const onSend = vi.fn();
    await twoLanded(onSend);
    fireEvent.change(field(), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSend).toHaveBeenCalledWith("![Image 1](uploads/paste-1-yynv.png)\n![Image 2](uploads/paste-2-z5cr.jpg)");
  });

  it("does not add a pill the reader removed", async () => {
    const onSend = vi.fn();
    await twoLanded(onSend);
    fireEvent.click(screen.getByRole("button", { name: "Remove blue-circle.png (Image 1)" }));
    fireEvent.change(field(), { target: { value: "just this one" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    // The survivor renumbered to Image 1 and rides after the words; the
    // removed one is on the message nowhere.
    expect(onSend).toHaveBeenCalledWith("just this one\n\n![Image 1](uploads/paste-2-z5cr.jpg)");
  });
});

describe("an SVG picked for the message", () => {
  it("is an image pill that uploads, like any other picture", () => {
    const { uploader, calls } = scriptedUploader();
    render(<Composer {...base()} uploader={uploader} />);
    const input = document.querySelector<HTMLInputElement>(".chat-composer-attachinput")!;
    const star = new File(['<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"/>'], "star.svg", {
      type: "image/svg+xml",
    });
    fireEvent.change(input, { target: { files: [star] } });
    expect(field().value).toBe("[Image 1] ");
    expect(calls).toHaveLength(1);
    expect(calls[0].kind).toBe("image");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
