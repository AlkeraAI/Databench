// Who may retire the installed chat runtime, and when it actually goes.
//
// The slot is read through a getter by everything in the chat composition, and
// the thing that decides its lifetime is a React provider whose effects React
// tears down and rebuilds without re-rendering it. Two rules make that safe,
// and both are invisible from the React tests because the provider's own
// re-install already covers the common ordering — so they are pinned here,
// where removing either one is a failure.

import { describe, expect, it } from "vitest";

import {
  chatData,
  chatRuntime,
  installChatRuntime,
  releaseChatRuntime,
  resetChatRuntime,
  type ChatDataSource,
  type ChatHost,
  type ChatRuntime,
} from "@/pages/workspace/chat/data";

/** A pair with nothing in it but its identity — the slot never calls into it. */
function pair(label: string): ChatRuntime {
  return {
    source: { label } as unknown as ChatDataSource,
    host: { label } as unknown as ChatHost,
  };
}

/** Let the release that was asked for actually happen. */
async function nextTask(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe("releasing the installed runtime", () => {
  it("does not take it away in the task the release was asked for", async () => {
    // THE DEFERRAL. React's re-attach runs the owner's cleanup, then every
    // CHILD effect, then the owner's own effect — child first. A child reading
    // the runtime in that window must still find it, so the release cannot be
    // applied where it is asked for.
    const owner = {};
    const runtime = pair("a");
    resetChatRuntime();
    installChatRuntime(runtime, owner);

    releaseChatRuntime(owner);

    expect(chatRuntime()).toBe(runtime);
    await nextTask();
    expect(() => chatRuntime()).toThrow(/no chat runtime installed/);
  });

  it("is cancelled by an install that lands before the release does", async () => {
    // The same owner coming straight back is a re-attach, not a departure.
    const owner = {};
    const runtime = pair("a");
    resetChatRuntime();
    installChatRuntime(runtime, owner);

    releaseChatRuntime(owner);
    installChatRuntime(runtime, owner);
    await nextTask();

    expect(chatRuntime()).toBe(runtime);
  });

  it("is cancelled by a DIFFERENT owner installing in between", async () => {
    // One subtree leaving as another arrives inside one task. The arriving pair
    // must survive the leaver's release.
    const leaving = {};
    const arriving = {};
    const second = pair("b");
    resetChatRuntime();
    installChatRuntime(pair("a"), leaving);

    releaseChatRuntime(leaving);
    installChatRuntime(second, arriving);
    await nextTask();

    expect(chatRuntime()).toBe(second);
  });
});

describe("who may retire it", () => {
  it("ignores a release from an owner that never installed", async () => {
    // THE OWNER GUARD. Two shells reach this slot — the browser's provider and
    // the extension webview's import-time install — and neither may pull the
    // pair out from under the other.
    const runtime = pair("shell");
    resetChatRuntime();
    installChatRuntime(runtime);

    releaseChatRuntime({});
    await nextTask();

    expect(chatRuntime()).toBe(runtime);
  });

  it("ignores a release from an owner that has since been replaced", async () => {
    const first = {};
    const second = pair("b");
    resetChatRuntime();
    installChatRuntime(pair("a"), first);
    installChatRuntime(second, {});

    releaseChatRuntime(first);
    await nextTask();

    expect(chatRuntime()).toBe(second);
  });

  it("retires only the pair its own owner installed", async () => {
    const owner = {};
    const runtime = pair("a");
    resetChatRuntime();
    installChatRuntime(runtime, owner);

    releaseChatRuntime(owner);
    await nextTask();

    expect(() => chatData()).toThrow(/no chat runtime installed/);
  });
});
