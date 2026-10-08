// A fake DOM WebSocket for the realtime client: the test opens it, speaks as the server, closes
// it with a code, and reads every frame the client wrote. It satisfies the same `Pick` of the
// real WebSocket the client drives, so the production factory and this one are interchangeable.

import { vi } from "vitest";

import { ApiError } from "@/api/errors";
import type { ClientFrame, WebSocketFactory, WebSocketLike, WsTicket } from "@/api/realtime/wsClient";

export class FakeWebSocket implements WebSocketLike {
  readyState = 0;
  onopen: WebSocket["onopen"] = null;
  onclose: WebSocket["onclose"] = null;
  onerror: WebSocket["onerror"] = null;
  onmessage: WebSocket["onmessage"] = null;
  readonly sent: string[] = [];
  closedWith: { code: number | undefined; reason: string | undefined } | null = null;

  constructor(
    readonly url: string,
    readonly protocols: string[],
  ) {}

  send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
    if (this.readyState !== 1) throw new Error("send on a socket that is not open");
    this.sent.push(String(data));
  }

  close(code?: number, reason?: string): void {
    if (this.readyState >= 2) return;
    this.readyState = 2;
    this.closedWith = { code, reason };
  }

  // -- the server's side ------------------------------------------------------

  /** The TCP handshake completed; the server has not yet spoken. */
  open(): void {
    this.readyState = 1;
    this.onopen?.call(this as unknown as WebSocket, new Event("open"));
  }

  /** The server writes one frame (an object is JSON-encoded; a string goes as is). */
  serverSend(frame: object | string): void {
    const data = typeof frame === "string" ? frame : JSON.stringify(frame);
    this.onmessage?.call(this as unknown as WebSocket, new MessageEvent("message", { data }));
  }

  /** The server closes with `code`. */
  serverClose(code: number, reason = ""): void {
    this.readyState = 3;
    this.onclose?.call(this as unknown as WebSocket, new CloseEvent("close", { code, reason }));
  }

  /** Open and greet: the handshake the client treats as "connected". */
  welcome(peerId = "p:1", limits?: Record<string, unknown>): void {
    this.open();
    this.serverSend({
      t: "welcome",
      peer_id: peerId,
      server_time: "2026-09-05T12:00:00Z",
      instance: "i1",
      ...(limits ? { limits } : {}),
    });
  }

  /** Every frame the client wrote, decoded. */
  frames(): ClientFrame[] {
    return this.sent.map((s) => JSON.parse(s) as ClientFrame);
  }

  framesOf<T extends ClientFrame["t"]>(t: T): Extract<ClientFrame, { t: T }>[] {
    return this.frames().filter((f): f is Extract<ClientFrame, { t: T }> => f.t === t);
  }
}

export function socketFactory() {
  const sockets: FakeWebSocket[] = [];
  const factory: WebSocketFactory = vi.fn((url: string, protocols: string[]) => {
    const socket = new FakeWebSocket(url, protocols);
    sockets.push(socket);
    return socket;
  });
  return {
    factory,
    sockets,
    last: (): FakeWebSocket => {
      const socket = sockets[sockets.length - 1];
      if (!socket) throw new Error("no socket was opened");
      return socket;
    },
  };
}

/** A ticket minter answering `T1`, `T2`, … in order; `refuse` makes the next mint fail. */
export function ticketMinter() {
  let n = 0;
  const minted: string[] = [];
  const refusals: unknown[] = [];
  const mint = vi.fn(async (): Promise<WsTicket> => {
    const refusal = refusals.shift();
    if (refusal !== undefined) throw refusal;
    n += 1;
    const ticket = `T${n}`;
    minted.push(ticket);
    return { ticket, expires_in: 30, path: "/api/v1/ws" };
  });
  return {
    mint,
    minted,
    refuseNext(error: unknown = new Error("mint failed")) {
      refusals.push(error);
    },
    refuseNextUnauthorized() {
      refusals.push(new ApiError(401, { detail: "unauthorized" }, "unauthorized"));
    },
  };
}

/** Run every timer and microtask due within `ms` of fake time. */
export async function advance(ms: number): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
}
