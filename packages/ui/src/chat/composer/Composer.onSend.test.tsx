// Uploads held until the send. An uploader whose chat does not exist yet says
// `timing: "on-send"`: a pasted, dropped or picked file becomes a pill the
// same way, but nothing goes up and Send stays open. Pressing Send opens the
// chat first (`beforeSend`, with the message the pills sit in), uploads every
// held pill under the same retries as an at-once one, and lets the message go
// once each has landed. Pinned here: the held pill, the order of open →
// upload → send, Send closed for the length of it, a failed pill withdrawn
// with its reason while the words still go, a pill-only message whose upload
// failed staying put, an open that fails keeping everything in the field, and
// a paste with no file in it doing nothing.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer } from "./Composer";
import type { ComposerUploader } from "./uploads";

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

interface Call {
  file: File;
  kind: string;
  n: number;
  resolve: (path: string) => void;
  reject: (e: Error) => void;
}

/** An on-send uploader whose open and uploads the test releases by hand. */
function heldUploader() {
  const calls: Call[] = [];
  const opens: Array<{ message: string; resolve: () => void; reject: (e: Error) => void }> = [];
  const uploader: ComposerUploader = {
    timing: "on-send",
    beforeSend: (message) =>
      new Promise<void>((resolve, reject) => {
        opens.push({ message, resolve, reject });
      }),
    upload: (file, hint) =>
      new Promise<{ path: string }>((resolve, reject) => {
        calls.push({ file, kind: hint.kind, n: hint.n, resolve: (path) => resolve({ path }), reject });
      }),
    retryDelayMs: () => 0,
  };
  return { uploader, calls, opens };
}

function field(): HTMLTextAreaElement {
  return screen.getByRole("textbox", { name: "Message Databench" }) as HTMLTextAreaElement;
}

function paste(files: File[]): void {
  fireEvent.paste(field(), { clipboardData: { files, getData: () => "" } });
}

function pills(): HTMLLIElement[] {
  return Array.from(screen.getByRole("list", { name: "Uploads" }).querySelectorAll("li"));
}

function sendKey(): HTMLElement {
  return screen.getByRole("button", { name: /^Send/ });
}

describe("uploads held until the send", () => {
  it("a pasted image becomes a held pill: nothing goes up, no chat is opened, Send stays open", () => {
    const { uploader, calls, opens } = heldUploader();
    render(<Composer {...base()} uploader={uploader} />);
    paste([png()]);
    expect(field().value).toBe("[Image 1] ");
    expect(pills()[0].getAttribute("data-state")).toBe("held");
    expect(screen.getByRole("img", { name: "Attached, uploads on send" })).toBeTruthy();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(calls).toHaveLength(0);
    expect(opens).toHaveLength(0);
    expect(sendKey().hasAttribute("disabled")).toBe(false);
    expect(sendKey().getAttribute("aria-label")).toBe("Send");
  });

  it("the attach door and a drop hold their files the same way", () => {
    const { uploader, calls } = heldUploader();
    render(<Composer {...base()} uploader={uploader} />);
    const input = document.querySelector<HTMLInputElement>(".chat-composer-attachinput")!;
    fireEvent.change(input, { target: { files: [csv("picked.csv")] } });
    fireEvent.drop(field().parentElement!, { dataTransfer: { files: [png("dropped.png")] } });
    expect(field().value).toBe("[File 1] [Image 1] ");
    expect(pills().map((p) => p.getAttribute("data-state"))).toEqual(["held", "held"]);
    expect(calls).toHaveLength(0);
  });

  it("a held pill whose token the reader typed over is still uploaded and still on the message", async () => {
    const { uploader, calls, opens } = heldUploader();
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} />);
    paste([png("a.png"), png("b.png")]);
    fireEvent.change(field(), { target: { value: "describe these images" } });
    fireEvent.click(sendKey());

    expect(opens[0].message).toBe("describe these images");
    await act(async () => opens[0].resolve());
    await waitFor(() => expect(calls).toHaveLength(2));
    await act(async () => {
      calls[0].resolve("uploads/paste-1-a.png");
      calls[1].resolve("uploads/paste-2-b.png");
    });
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend).toHaveBeenCalledWith(
      "describe these images\n\n![Image 1](uploads/paste-1-a.png)\n![Image 2](uploads/paste-2-b.png)",
    );
  });

  it("Send opens the chat for the words first, then uploads every held pill, then sends the markdown", async () => {
    const { uploader, calls, opens } = heldUploader();
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} />);
    fireEvent.change(field(), { target: { value: "compare " } });
    field().setSelectionRange(8, 8);
    paste([png("shot.png"), csv("numbers.csv")]);
    fireEvent.change(field(), { target: { value: `${field().value}please` } });
    fireEvent.click(sendKey());

    // The open is asked with the reader's words and none of the pills; no
    // upload has started and Send is closed for the length of it.
    expect(opens).toHaveLength(1);
    expect(opens[0].message).toBe("compare please");
    expect(calls).toHaveLength(0);
    expect(onSend).not.toHaveBeenCalled();
    await waitFor(() => expect(sendKey().hasAttribute("disabled")).toBe(true));
    expect(sendKey().getAttribute("aria-label")).toBe("Send: Waiting for uploads to finish");

    await act(async () => opens[0].resolve());
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls.map((c) => `${c.kind}${c.n}`)).toEqual(["image1", "file1"]);
    expect(pills().map((p) => p.getAttribute("data-state"))).toEqual(["uploading", "uploading"]);
    expect(sendKey().hasAttribute("disabled")).toBe(true);
    // The words are still in the field while the bytes go up: nothing left yet.
    expect(field().value).toBe("compare [Image 1] [File 1] please");
    expect(onSend).not.toHaveBeenCalled();

    await act(async () => {
      calls[0].resolve("paste-1-ab12.png");
      calls[1].resolve("file-1-cd34.csv");
    });
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend).toHaveBeenCalledWith(
      "compare ![Image 1](paste-1-ab12.png) [File 1: numbers.csv](file-1-cd34.csv) please",
    );
    expect(field().value).toBe("");
    expect(screen.queryByRole("list", { name: "Uploads" })).toBeNull();
    // The key closes for an empty field, not for one that only held uploads.
    expect(sendKey().getAttribute("aria-label")).toBe("Send");
  });

  it("Enter commits the held pills the same way the key does", async () => {
    const { uploader, calls, opens } = heldUploader();
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} />);
    paste([png()]);
    fireEvent.keyDown(field(), { key: "Enter" });
    expect(opens).toHaveLength(1);
    await act(async () => opens[0].resolve());
    await waitFor(() => expect(calls).toHaveLength(1));
    await act(async () => calls[0].resolve("paste-1-x.png"));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("![Image 1](paste-1-x.png)"));
  });

  it("a second press while the chat is being opened opens nothing twice and sends once", async () => {
    const { uploader, calls, opens } = heldUploader();
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} />);
    paste([png()]);
    fireEvent.click(sendKey());
    fireEvent.click(sendKey());
    fireEvent.keyDown(field(), { key: "Enter" });
    expect(opens).toHaveLength(1);
    await act(async () => opens[0].resolve());
    await waitFor(() => expect(calls).toHaveLength(1));
    await act(async () => calls[0].resolve("paste-1-x.png"));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(opens).toHaveLength(1);
  });

  it("a held pill that fails for good is withdrawn with its reason, and the words still go", async () => {
    const { uploader, calls, opens } = heldUploader();
    const onSend = vi.fn();
    const onUploadFailed = vi.fn();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} onUploadFailed={onUploadFailed} />);
    fireEvent.change(field(), { target: { value: "look " } });
    field().setSelectionRange(5, 5);
    paste([png("a.png"), csv("q.csv")]);
    fireEvent.click(sendKey());
    await act(async () => opens[0].resolve());
    await waitFor(() => expect(calls).toHaveLength(2));
    await act(async () => calls[1].resolve("file-1-x.csv"));
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await waitFor(() => expect(calls.filter((c) => c.kind === "image")).toHaveLength(attempt + 1));
      const call = calls.filter((c) => c.kind === "image")[attempt];
      await act(async () => call.reject(new Error("quota exceeded")));
    }
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend).toHaveBeenCalledWith("look [File 1: q.csv](file-1-x.csv)");
    expect(onUploadFailed).toHaveBeenCalledWith("a.png", "quota exceeded");
    expect(screen.getByRole("alert").textContent).toBe("a.png could not be uploaded: quota exceeded");
    expect(calls.filter((c) => c.kind === "image")).toHaveLength(3);
    expect(field().value).toBe("");
  });

  it("a pill that was the whole message stays in the field with the reason when its upload fails; nothing is sent", async () => {
    const { uploader, calls, opens } = heldUploader();
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} />);
    paste([png("a.png")]);
    fireEvent.click(sendKey());
    await act(async () => opens[0].resolve());
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await waitFor(() => expect(calls).toHaveLength(attempt + 1));
      await act(async () => calls[attempt].reject(new Error("store down")));
    }
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("a.png could not be uploaded: store down"));
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.queryByRole("list", { name: "Uploads" })).toBeNull();
    expect(field().value).toBe("");
    // Closed for the empty field only: the commit is over.
    await waitFor(() => expect(sendKey().getAttribute("aria-label")).toBe("Send"));
    // The reader pastes again: the pill goes through the same door, and Send
    // is theirs again.
    paste([png("b.png")]);
    expect(pills()[0].getAttribute("data-state")).toBe("held");
    expect(sendKey().hasAttribute("disabled")).toBe(false);
  });

  it("an open that fails keeps the words and every pill, states the reason, uploads nothing, sends nothing", async () => {
    const { uploader, calls, opens } = heldUploader();
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} />);
    fireEvent.change(field(), { target: { value: "see " } });
    field().setSelectionRange(4, 4);
    paste([png("a.png")]);
    fireEvent.click(sendKey());
    await act(async () => opens[0].reject(new Error("the workspace has no machine")));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe("Couldn't start the chat: the workspace has no machine"),
    );
    expect(field().value).toBe("see [Image 1] ");
    expect(pills()[0].getAttribute("data-state")).toBe("held");
    expect(calls).toHaveLength(0);
    expect(onSend).not.toHaveBeenCalled();
    expect(sendKey().hasAttribute("disabled")).toBe(false);
    // Pressing again asks the open again — the earlier refusal is not sticky.
    fireEvent.click(sendKey());
    expect(opens).toHaveLength(2);
  });

  it("a paste that carries no file is left to the browser: no pill, no open", () => {
    const { uploader, opens } = heldUploader();
    render(<Composer {...base()} uploader={uploader} />);
    fireEvent.change(field(), { target: { value: "plain " } });
    const event = fireEvent.paste(field(), {
      clipboardData: { files: [], getData: () => "some text" },
    });
    // Not prevented: the browser's own text paste goes ahead.
    expect(event).toBe(true);
    expect(field().value).toBe("plain ");
    expect(screen.queryByRole("list", { name: "Uploads" })).toBeNull();
    expect(opens).toHaveLength(0);
  });

  it("a held pill removed before the send is never uploaded", async () => {
    const { uploader, calls, opens } = heldUploader();
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} uploader={uploader} />);
    fireEvent.change(field(), { target: { value: "hi " } });
    field().setSelectionRange(3, 3);
    paste([png("a.png"), png("b.png")]);
    fireEvent.click(screen.getByRole("button", { name: "Remove a.png (Image 1)" }));
    expect(field().value).toBe("hi [Image 1] ");
    fireEvent.click(sendKey());
    await act(async () => opens[0].resolve());
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].file.name).toBe("b.png");
    expect(calls[0].n).toBe(1);
  });
});
