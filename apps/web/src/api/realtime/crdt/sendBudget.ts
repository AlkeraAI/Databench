// The share of a socket's inbound budget the live lane allows itself.
//
// The server closes a socket that sends more than its window allows (`4429`),
// and the window is the whole socket's: subscribes, presence beats, carets and
// every live update together. The lane keeps to half of it, so a burst of
// typing can never be what closes the socket under everything else on it. A
// send that would overrun waits; it is never dropped — the lane coalesces
// whatever was typed meanwhile into the next update.

import type { SocketLimits } from "../wsClient";

/** What a server that predates `welcome.limits` holds a person's socket to. */
export const FALLBACK_LIMITS: SocketLimits = {
  frames_per_window: 200,
  bytes_per_window: 4 * 1024 * 1024,
  window_seconds: 10,
  max_frame_bytes: 2 * 1024 * 1024 + 64 * 1024,
};

/** The share of the socket's window the lane may use. */
export const LANE_SHARE = 0.5;

export class SendBudget {
  private readonly sent: { at: number; bytes: number }[] = [];

  constructor(
    private readonly limits: () => SocketLimits | null,
    private readonly now: () => number = () => Date.now(),
  ) {}

  private window(): { frames: number; bytes: number; ms: number } {
    const limits = this.limits() ?? FALLBACK_LIMITS;
    return {
      frames: Math.max(1, Math.floor(limits.frames_per_window * LANE_SHARE)),
      bytes: Math.max(1, Math.floor(limits.bytes_per_window * LANE_SHARE)),
      ms: limits.window_seconds * 1000,
    };
  }

  private prune(ms: number, now: number): void {
    while (this.sent.length > 0 && this.sent[0]!.at <= now - ms) this.sent.shift();
  }

  /** Milliseconds until `frames` frames carrying `bytes` may go; 0 when now. */
  waitFor(frames: number, bytes: number): number {
    const { frames: maxFrames, bytes: maxBytes, ms } = this.window();
    const now = this.now();
    this.prune(ms, now);
    let usedFrames = this.sent.length;
    let usedBytes = this.sent.reduce((n, s) => n + s.bytes, 0);
    if (usedFrames + frames <= maxFrames && usedBytes + bytes <= maxBytes) return 0;
    // The earliest moment enough of the window has rolled off.
    for (const entry of this.sent) {
      usedFrames -= 1;
      usedBytes -= entry.bytes;
      if (usedFrames + frames <= maxFrames && usedBytes + bytes <= maxBytes) {
        return entry.at + ms - now + 1;
      }
    }
    // Larger than the whole share: wait out the window and send it alone.
    return ms;
  }

  /** Record `frames` frames carrying `bytes` as sent now. */
  spend(frames: number, bytes: number): void {
    const now = this.now();
    for (let i = 0; i < frames; i += 1) this.sent.push({ at: now, bytes: i === 0 ? bytes : 0 });
  }
}

const budgets = new WeakMap<object, SendBudget>();

/** The one budget every live channel on `socket` shares. */
export function budgetFor(socket: { readonly limits: SocketLimits | null }): SendBudget {
  let budget = budgets.get(socket);
  if (budget === undefined) {
    budget = new SendBudget(() => socket.limits);
    budgets.set(socket, budget);
  }
  return budget;
}
