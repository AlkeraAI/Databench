// The browser's side of the chat composition's host port.
//
// A page has no editor and no extension command to run. Where the port allows
// it, that is said by OMITTING the verb rather than by implementing it as a
// no-op — a missing `openFile` keeps a path in a tool card a label instead of a
// dead button, which is the difference between a shell that admits what it
// cannot do and one that looks the same and does nothing. What the browser CAN
// do it does properly: a save is a real download, the account line comes from
// the session the portal already holds, and the opening screen asks the
// question a reader with a warehouse actually arrives with.

import type { SuggestedPrompt } from "@alkera/chat-model";
import { openExternalUrl } from "@alkera/ui";

import type { ChatAccount, ChatEmptyState, ChatHost } from "./ChatDataSource";

/** What a browser reader is asked before there is a transcript.
 *
 *  They have a warehouse, not a working tree, so the questions are the ones an
 *  analyst arrives with. The composition's own opening (prompts about the
 *  codebase and "the file I have open") is the editor's, and would send the
 *  agent to tour a repository the reader has never seen. */
const BROWSER_PROMPTS: SuggestedPrompt[] = [
  {
    id: "p1",
    label: "What changed this week?",
    prefill: "What changed in my data this week, and what explains the change?",
  },
  {
    id: "p2",
    label: "Find the biggest movers",
    prefill: "Which metrics moved the most over the last month? Show the change.",
  },
  {
    id: "p3",
    label: "Describe my tables",
    prefill: "Which tables can you see, and what does each one hold?",
  },
];

const BROWSER_EMPTY_STATE: ChatEmptyState = {
  title: "Ask a question about your data",
  prompts: BROWSER_PROMPTS,
  placeholder: "Ask a question about your data…",
};

export interface BrowserChatHostOptions {
  /** Who the portal has signed in, re-read whenever the account changes. */
  account?: () => ChatAccount;
  /** Where a saved file goes; the default writes a download in the browser. */
  save?: (suggestedName: string, contents: string, mimeType?: string) => void;
}

const NO_ACCOUNT: ChatAccount = { email: null, webAppUrl: null };

function download(suggestedName: string, contents: string, mimeType = "text/plain"): void {
  const url = URL.createObjectURL(new Blob([contents], { type: mimeType }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = suggestedName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/** The chat composition's shell services, as the browser portal provides them. */
export function createBrowserChatHost(opts: BrowserChatHostOptions = {}): ChatHost {
  const readAccount = opts.account ?? (() => NO_ACCOUNT);
  const save = opts.save ?? download;
  const listeners = new Set<() => void>();
  return {
    kind: "browser",
    runCommand: async () => undefined,
    // No `openFile` at all, deliberately: a page has no editor to open one in.
    // The absence is what keeps a path in a tool card a label rather than a
    // button that answers a click with nothing.
    workspacePath: () => null,
    emptyState: BROWSER_EMPTY_STATE,
    saveFile: async (suggestedName, contents, mimeType) => {
      save(suggestedName, contents, mimeType);
    },
    openPlanDocument: async () => undefined,
    subscribe: () => () => undefined,
    account: readAccount,
    /** The portal re-renders on its own when the session changes, so there is
     *  nothing to push; the subscription exists so the port has one shape. */
    onAccountChange: (listener) => {
      listeners.add(listener);
      return () => void listeners.delete(listener);
    },
    // These URLs come from permission asks and web-search hits, so they carry
    // model- and third-party text. The shared floor is what decides whether one
    // becomes a navigation at all.
    auth: { openBrowser: (url) => void openExternalUrl(url) },
    engine: {
      request: () => Promise.reject(new Error("the browser portal has no engine channel")),
    },
  };
}
