// The chat's shared composer draft, as one hook the dock can hand to a Composer.
//
// A cloud chat is a document several people can have open, so what is half
// typed into it belongs to the chat rather than to the tab. It lives on the
// live lane: a Loro text the Composer's field is bound to keystroke by
// keystroke (`binding`), with other people's carets and edits arriving without
// moving this reader's caret.
//
// Until the live draft is ready the field is this tab's own, and what is typed
// there is kept here and carried onto the live draft when it arrives. Where the
// live draft cannot run, the field stays this tab's own and says why: a browser
// with no WebAssembly cannot run Loro and keeps it so for as long as the
// composer is open, while a lane that failed for any other reason (the server
// refused it, or Loro's code did not download) is asked again after a doubling
// wait, carrying what was typed meanwhile when it answers. A browser that is
// offline says nothing here (the dock's own note says it is reconnecting) and
// asks again the moment the network is back. A chat the reader may not see has
// no shared draft and says nothing.
//
// A source with no shared draft at all (the editor's daemon: one person, one
// keyboard) implements none of this and the hook returns nothing.

import { useEffect, useRef, useState } from "react";

import { spliceFor, type TextBinding } from "@alkera/ui";

import { ladderDelay } from "../../../lib/limits";
import { browserOnline, onBrowserOnline } from "../../../lib/online";
import { rebaseSplice } from "../../../api/realtime/crdt/textBinding";
import type { LiveDraftState } from "../../../api/realtime/crdt/liveDraft";
import { chatData } from "./data";

/** What a Composer needs to show and report a shared draft. */
export interface SharedDraftBinding {
  /** Every local keystroke while the field is this tab's own, so it can be
   *  carried onto the live draft when that arrives. */
  onDraftChange?: (text: string) => void;
  /** Why the field is this tab's own rather than the chat's. */
  draftNotice?: string;
  /** The live lane's binding: when set, the composer is a view of the live
   *  draft and the two above are not used. */
  binding?: TextBinding;
}

/** Shown when this browser cannot load the live lane. */
export const LIVE_SYNC_UNSUPPORTED = "This browser does not support our live sync.";
/** Shown while the server cannot serve the live lane. */
export const LIVE_SYNC_UNAVAILABLE = "Live sync is unavailable right now.";

/** The first wait before asking the server for the live lane again, and the
 *  longest; each failed ask doubles the wait. */
export const REJOIN_FIRST_MS = 30_000;
export const REJOIN_MAX_MS = 5 * 60_000;
const REJOIN = { floorMs: REJOIN_FIRST_MS, capMs: REJOIN_MAX_MS };

/** Codes that say the reader may not see this chat's draft at all. */
const ABSENT = new Set(["not_found", "bad_channel", "forbidden"]);

type LocalOnly = "browser" | "unavailable" | "absent";

/** Whether this browser can run Loro at all. A load that failed in one that
 *  can is a download that did not arrive (offline, a flaky network, a chunk
 *  replaced by a deploy), which passes; telling the reader their browser is
 *  unsupported for it was wrong, and it never cleared. */
function canRunLoro(): boolean {
  return typeof WebAssembly !== "undefined";
}

function localOnlyFor(reason: string): LocalOnly {
  if (reason === "load_failed") return canRunLoro() ? "unavailable" : "browser";
  if (ABSENT.has(reason)) return "absent";
  return "unavailable";
}

export function useSharedDraft(chatId: string | null): SharedDraftBinding {
  const [live, setLive] = useState<LiveDraftState>({ kind: "pending" });
  // Why the field is this tab's own, kept across a retry; unread while live.
  const [localOnly, setLocalOnly] = useState<LocalOnly | null>(null);
  // Bumped to ask for the live lane again.
  const [reopen, setReopen] = useState(0);
  // What the field held when it became this tab's own, and what it holds now:
  // only the edit between the two is carried onto the live draft.
  const typed = useRef<{ base: string; text: string } | null>(null);
  // The live draft that took the field over, and what the field held then:
  // until React draws the bound composer, a key still reaches the local
  // handler, and it is passed on to the live draft from here.
  const handoff = useRef<{ binding: TextBinding; local: string } | null>(null);
  const retries = useRef(0);
  const [online, setOnline] = useState(browserOnline);
  // The lane is fallen back, so the network coming back is worth an ask now
  // rather than at the end of the doubling wait.
  const fallenBack = useRef(false);

  useEffect(() => {
    const went = (): void => setOnline(false);
    const came = (): void => {
      setOnline(true);
      if (!fallenBack.current) return;
      retries.current = 0;
      setReopen((n) => n + 1);
    };
    return onBrowserOnline({ offline: went, online: came });
  }, []);

  // Before the lane effect below, which runs after it on a chat change.
  useEffect(() => {
    typed.current = null;
    handoff.current = null;
    retries.current = 0;
    setLocalOnly(null);
  }, [chatId]);

  useEffect(() => {
    setLive({ kind: "pending" });
    if (!chatId) return;
    const source = chatData();
    if (!source.openLiveDraft) return;
    const { draft, release } = source.openLiveDraft(chatId);
    let wasLive = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    const stop = draft.subscribe((state) => {
      fallenBack.current = state.kind === "fallback";
      if (state.kind === "live" && !wasLive) {
        wasLive = true;
        retries.current = 0;
        const pending = typed.current;
        typed.current = null;
        if (pending !== null) carry(state.binding, pending);
        handoff.current = { binding: state.binding, local: pending?.text ?? "" };
      }
      if (state.kind === "fallback") {
        if (wasLive) {
          // The field keeps what the live draft showed; typing from here on is
          // an edit of that.
          wasLive = false;
          handoff.current = null;
          const shown = state.fallback.localText;
          typed.current = { base: shown, text: shown };
        }
        const why = localOnlyFor(state.fallback.reason);
        setLocalOnly(why);
        if (why === "unavailable" && retryTimer === null) {
          const wait = ladderDelay(retries.current, REJOIN);
          retries.current += 1;
          retryTimer = setTimeout(() => setReopen((n) => n + 1), wait);
        }
      }
      setLive(state);
    });
    return () => {
      if (retryTimer !== null) clearTimeout(retryTimer);
      stop();
      release();
    };
  }, [chatId, reopen]);

  if (!chatId || !chatData().openLiveDraft) return {};
  if (live.kind === "live") return { binding: live.binding };
  if (localOnly === "absent") return {};
  return {
    onDraftChange: (text: string) => {
      const live = handoff.current;
      if (live !== null) {
        carry(live.binding, { base: live.local, text });
        live.local = text;
        return;
      }
      typed.current = { base: typed.current?.base ?? "", text };
    },
    // Offline, the dock's own line says it is reconnecting; a second line
    // under the field blaming the lane would say the same thing worse.
    draftNotice: !online
      ? undefined
      : localOnly === "browser"
        ? LIVE_SYNC_UNSUPPORTED
        : localOnly === "unavailable"
          ? LIVE_SYNC_UNAVAILABLE
          : undefined,
  };
}

/** Carry what was typed while the field was this tab's own onto the live
 *  draft: the edit since what the field started from, moved onto the live text. */
function carry(binding: TextBinding, pending: { base: string; text: string }): void {
  const edit = spliceFor(pending.base, pending.text);
  if (edit === null) return;
  const shown = binding.text();
  const moved = rebaseSplice(pending.base, shown, edit);
  if (moved === null) return;
  const after = shown.slice(0, moved.index) + moved.insert + shown.slice(moved.index + moved.remove);
  binding.edit(shown, after, null);
}
