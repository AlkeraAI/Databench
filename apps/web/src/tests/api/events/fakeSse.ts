// A scripted `fetch` for the event-stream client: every answer is decided by the test — a
// stream it can push text into, a refusal with a status, a thrown network error — and every
// request is recorded with its url, init and headers. The abort signal is honoured (a stream
// errors when the client aborts) so stall and stop paths are provable.

import { vi } from "vitest";

import type { FetchLike } from "@/api/events/sseClient";

export interface FakeStream {
  response: Response;
  /** Enqueue text on the open stream (any chunking — the parser must cope). */
  push(text: string): void;
  /** End the stream cleanly, as the server does at its deadline. */
  end(): void;
  /** Fail the stream mid-flight, as a dropped connection does. */
  fail(reason?: unknown): void;
  /** True once the client aborted the request (a stop, or a stall). */
  readonly aborted: boolean;
}

export function openStream(status = 200, headers: Record<string, string> = {}): FakeStream {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  let done = false;
  let aborted = false;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  const response = new Response(body, {
    status,
    headers: { "content-type": "text/event-stream", ...headers },
  });
  const stream: FakeStream & { abort(): void } = {
    response,
    push: (text) => {
      if (!done) controller.enqueue(encoder.encode(text));
    },
    end: () => {
      if (done) return;
      done = true;
      controller.close();
    },
    fail: (reason = new Error("stream failed")) => {
      if (done) return;
      done = true;
      controller.error(reason);
    },
    abort: () => {
      aborted = true;
      if (done) return;
      done = true;
      controller.error(new DOMException("The operation was aborted.", "AbortError"));
    },
    get aborted() {
      return aborted;
    },
  };
  return stream;
}

export interface RecordedRequest {
  url: string;
  method: string;
  credentials: RequestCredentials;
  headers: Record<string, string>;
  request: Request;
  init: RequestInit | undefined;
}

function headersOf(headers: Headers): Record<string, string> {
  const out: Record<string, string> = {};
  headers.forEach((value, key) => {
    out[key.toLowerCase()] = value;
  });
  return out;
}

export function scriptedFetch() {
  const requests: RecordedRequest[] = [];
  const answers: Array<(signal: AbortSignal | null | undefined) => Promise<Response>> = [];
  const fetch = vi.fn<FetchLike>(async (input, init) => {
    requests.push({
      url: input.url,
      method: input.method,
      credentials: input.credentials,
      headers: headersOf(input.headers),
      request: input,
      init,
    });
    const next = answers.shift();
    if (!next) throw new Error(`unscripted fetch #${requests.length} to ${input.url}`);
    return next(init?.signal ?? input.signal);
  });
  return {
    fetch,
    requests,
    /** The next request is answered with this stream; the client's abort ends it. */
    answerStream(stream: FakeStream) {
      answers.push(async (signal) => {
        signal?.addEventListener("abort", () => (stream as FakeStream & { abort(): void }).abort());
        return stream.response;
      });
    },
    /** The next request is refused with a plain JSON body at `status`. */
    answerStatus(status: number, headers: Record<string, string> = {}) {
      answers.push(
        async () =>
          new Response(JSON.stringify({ detail: `refused ${status}` }), {
            status,
            headers: { "content-type": "application/json", ...headers },
          }),
      );
    },
    /** The next request fails before any response (DNS, a dropped socket). */
    answerThrow(error: unknown = new TypeError("network down")) {
      answers.push(async () => {
        throw error;
      });
    },
    /** The next request resolves with something that is not a Response at all. */
    answerGarbage() {
      answers.push(async () => undefined as unknown as Response);
    },
  };
}

/** Run every timer and microtask due within `ms` of fake time. */
export async function advance(ms: number): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
}
