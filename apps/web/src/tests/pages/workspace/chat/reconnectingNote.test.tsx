// The one thing a chat says while the browser has no network.
//
// A chat is the surface that is SUPPOSED to sit still: the reader sent
// something and is watching an answer arrive a token at a time. A connection
// that drops looks exactly like a model that is thinking, so the reader waits
// on an answer that is never coming. The note tells the two apart.
//
// Both halves of the timing are the point. It must not appear on the drop —
// a laptop hopping access points is offline for a second or two and the reader
// never needed to know — and it must go the instant the network is back,
// without anything to dismiss.

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  RECONNECTING_AFTER_MS,
  RECONNECTING_NOTE,
  ReconnectingNote,
} from "@/pages/workspace/chat/ReconnectingNote";

/** The browser's own answer to "am I connected", which is what the note reads
 *  and what the two events announce a change to. */
function setOnline(online: boolean): void {
  vi.spyOn(navigator, "onLine", "get").mockReturnValue(online);
}

function drop(): void {
  setOnline(false);
  act(() => {
    window.dispatchEvent(new Event("offline"));
  });
}

function restore(): void {
  setOnline(true);
  act(() => {
    window.dispatchEvent(new Event("online"));
  });
}

function wait(ms: number): void {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the note a chat shows while the browser is offline", () => {
  it("says nothing at all on a connected browser", () => {
    vi.useFakeTimers();
    setOnline(true);
    render(<ReconnectingNote />);

    expect(screen.queryByText(RECONNECTING_NOTE)).toBeNull();
    // Not hidden — absent: a note that is always in the DOM reserves space on
    // every chat that never drops.
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("waits out a hop between access points rather than flickering", () => {
    vi.useFakeTimers();
    setOnline(true);
    render(<ReconnectingNote />);

    drop();
    wait(RECONNECTING_AFTER_MS - 1);
    expect(screen.queryByText(RECONNECTING_NOTE)).toBeNull();

    // And a drop that resolves inside the wait is never announced, even once
    // the delay would have elapsed.
    restore();
    wait(RECONNECTING_AFTER_MS * 2);
    expect(screen.queryByText(RECONNECTING_NOTE)).toBeNull();
  });

  it("says it once the browser has really been offline", () => {
    vi.useFakeTimers();
    setOnline(true);
    render(<ReconnectingNote />);

    drop();
    wait(RECONNECTING_AFTER_MS);

    const note = screen.getByText(RECONNECTING_NOTE);
    // A live region, so a reader who is not looking at the dock is told —
    // `status` and not `alert`: it is a condition, not an interruption.
    expect(note.getAttribute("role")).toBe("status");
  });

  it("goes as soon as the network is back, with nothing to dismiss", () => {
    vi.useFakeTimers();
    setOnline(true);
    render(<ReconnectingNote />);

    drop();
    wait(RECONNECTING_AFTER_MS);
    expect(screen.getByText(RECONNECTING_NOTE)).toBeTruthy();
    // Nothing to press: it is not a toast, so it carries no close key.
    expect(screen.queryByRole("button")).toBeNull();

    restore();
    expect(screen.queryByText(RECONNECTING_NOTE)).toBeNull();
  });

  it("is already waiting on a tab opened while the network was down", () => {
    // The events only announce CHANGES, so a mount that reads no event has to
    // read the state instead — otherwise a reader who opens a chat on a dead
    // connection is told nothing until it comes back and drops again.
    vi.useFakeTimers();
    setOnline(false);
    render(<ReconnectingNote />);

    expect(screen.queryByText(RECONNECTING_NOTE)).toBeNull();
    wait(RECONNECTING_AFTER_MS);
    expect(screen.getByText(RECONNECTING_NOTE)).toBeTruthy();
  });
});
