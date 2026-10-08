// Where the realtime transports stand, for the surfaces that adapt to it.
//
// A module-level store (the knowledge and dashboard seams use the same shape)
// rather than React context: the polling predicates live inside data hooks
// far from the app root, and non-React code (a scheduler, a test) needs a
// synchronous read. Two rows — the event stream and the socket — each with the
// same vocabulary as the clients that drive them.

import { create } from "zustand";

import type { SseStatus } from "./sseClient";

export type RealtimeStatus = SseStatus;

interface RealtimeStatusState {
  /** The server event stream (`GET /api/v1/events`). */
  sse: RealtimeStatus;
  /** The realtime socket (`WS /api/v1/ws`). */
  ws: RealtimeStatus;
  setSse: (status: RealtimeStatus) => void;
  setWs: (status: RealtimeStatus) => void;
}

const INITIAL = { sse: "idle", ws: "idle" } as const;

export const useRealtimeStatus = create<RealtimeStatusState>((set) => ({
  ...INITIAL,
  setSse: (sse) => set({ sse }),
  setWs: (ws) => set({ ws }),
}));

/** True only while a transport is connected and delivering. */
export function isStreamLive(status: RealtimeStatus): boolean {
  return status === "connected";
}

/**
 * Whether the polling fallback should run: true whenever the event stream is NOT delivering —
 * before it first connects, while it reconnects, after it gave up, and when nobody is signed
 * in. Framed as "not live" rather than "given up" on purpose: a poll that runs during a
 * two-second reconnect costs one request; a poll that waits for the client to give up leaves a
 * five-second window in which nothing refreshes and nothing says so.
 */
export function isRealtimeDown(): boolean {
  return !isStreamLive(useRealtimeStatus.getState().sse);
}

/** The React form of {@link isRealtimeDown}; re-renders the caller when the answer flips. */
export function useRealtimeDown(): boolean {
  return useRealtimeStatus((s) => !isStreamLive(s.sse));
}

/** Back to the initial state — a test's `beforeEach`. */
export function resetRealtimeStatus(): void {
  useRealtimeStatus.setState({ ...INITIAL });
}
