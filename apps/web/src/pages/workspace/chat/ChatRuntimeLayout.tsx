// The chat runtime, owned by one React subtree: the chat and everything stacked
// over it.
//
// The source and the host are also a module-level installation, because the
// surfaces read them through `chatData()` rather than through a prop — a
// composition shared with the extension cannot import either shell. That getter
// is a convenience, not the owner: what OWNS the pair is the provider below,
// which builds it once per instance, hands it down as context, and keeps the
// getter pointed at it for exactly as long as the subtree is mounted.
//
// The install sits ABOVE every surface on the route they share (the chat, the
// results list, a plan, a compaction summary), so a cold load of any of them
// has a runtime, and moving between them neither tears it down nor builds a
// second source, which would be a second subscription that has never read the
// chat.
//
// React re-attaches a subtree's effects without re-rendering the parent that
// owns them (StrictMode's dev remount, a Suspense re-reveal, an error-boundary
// reset, a keyed remount), so a teardown can run before the re-install while a
// child effect such as `useComposerPrefs` still reads the host. The lifetime is
// therefore decided in `releaseChatRuntime`, not where the runtime is read.

import {
  createContext,
  useContext,
  useLayoutEffect,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { Outlet } from "react-router-dom";

import { useCurrentUser } from "../../../api/auth";
import { useChat, useCurrentMachine, type ChatSessionRead, type MachineStateRead } from "../../../api/chats";
import type { ChatMachineStatus } from "../../../api/cloudChat/transport";
import type { StatusFact } from "../../../api/status";
import { usePublisherGone } from "../../../api/realtime/publisherLinks";

import { chatAccountScope } from "../../../lib/accountScope";

import { installChatRuntime, releaseChatRuntime, type ChatAccount, type ChatRuntime } from "./data";
import { CloudDataSource } from "./data/CloudDataSource";
import { createBrowserChatHost } from "./data/browserChatHost";

/** The chat's own view of the machine.
 *
 *  The LIVE machine is the fact — its status is derived from the heartbeat it
 *  is sending right now, and `compute_machine.changed` invalidates it — so when
 *  the chat is bound to the machine the org is running, that machine's word
 *  wins. The chat row carries the binding and the one thing the machine alone
 *  can say: that it is running but was refused this chat's transcript.
 *
 *  Preferring the row is how a killed box kept reading "ready" (a composer
 *  nobody is listening to) and a chat opened while the box was booting stayed
 *  "starting" forever. A chat bound to some OTHER machine still reads its own
 *  row: it must not borrow the current box's health. */
export function useMachineStatus(
  chatId: string | undefined,
  newChatWorkspaceId?: string | null,
  { enabled = true }: { enabled?: boolean } = {},
): MachineStatusSeen {
  const seen = useMachineStatusFromRows(chatId, newChatWorkspaceId, enabled);
  // The box's socket went away and it has not come back: the server said so
  // the moment it happened, most of a minute before the heartbeat's window
  // would turn the machine's status. Only over a status that still says
  // ready: asleep, starting, restarting and the rest already say more.
  const publisherGone = usePublisherGone(chatId);
  if (publisherGone && seen.status === "ready") return { ...seen, status: "unreachable" };
  return seen;
}

export interface MachineStatusSeen {
  /** The machine's raw word, for what the page lets the reader do. */
  status: ChatMachineStatus | undefined;
  /** What the page says about it: the status the server wrote. A chat on a
   *  machine reads its own; one not placed yet, or a new chat, reads what
   *  stands between it and the machine its message would be placed on. */
  fact: StatusFact | null;
}

function useMachineStatusFromRows(
  chatId: string | undefined,
  newChatWorkspaceId: string | null | undefined,
  enabled: boolean,
): MachineStatusSeen {
  const chat = useChat(chatId);
  // A chat not placed yet runs where its workspace's chats run: a workspace
  // pinned to an org machine places it there, not where the org's other
  // chats go. A new chat asks for the workspace it will be made in.
  const place = chatId ? (chat.data && !chat.data.machine_id ? chat.data.workspace_id : null) : newChatWorkspaceId;
  const machine = useCurrentMachine(place, { enabled });
  if (chat.data) {
    const placed = Boolean(chat.data.machine_id);
    const fact = placed
      ? (chat.data.status ?? null)
      : (machine.data?.status_fact ?? chat.data.status ?? null);
    return { ...rawStatus(chat.data, machine.data), fact };
  }
  return {
    status: machine.data?.status,
    fact: machine.data?.status_fact ?? null,
  };
}

/** The machine's raw word for a chat: the live machine's where the chat is
 *  bound to it, else the chat row's own. */
function rawStatus(
  chat: ChatSessionRead,
  live: MachineStateRead | undefined,
): { status: ChatMachineStatus | undefined } {
  if (chat.machine_status === "refused") {
    return { status: "refused" };
  }
  if (live && live.machine_id && live.machine_id === chat.machine_id) {
    // The box is answering, and it said it holds no session for THIS chat:
    // the machine's heartbeat cannot see that, only the row can. Any other
    // word from the live machine (starting, gone, unreachable) still wins —
    // a chat asleep on a dead box is a dead box.
    if (live.status === "ready" && chat.machine_status === "asleep") {
      return { status: "asleep" };
    }
    return { status: live.status };
  }
  // A chat that has not been placed on a box yet — a copy, or one made while
  // nothing was up — reads the machine the ORG is running, because that is
  // the box that will take it: placement runs again when a machine reaches
  // ready, and again on the chat's first message. Reading its own empty
  // binding instead is what told a reader their organization has no workspace
  // while the box serving every other chat they own was answering.
  if (!chat.machine_id && live?.machine_id && live.status !== "none") {
    return { status: live.status };
  }
  // The same for an org the shared pool serves: its next message places the
  // chat on a pool box, so the chat is composable rather than "no workspace".
  if (!chat.machine_id && live?.status === "pool") {
    return { status: "pool" };
  }
  return { status: chat.machine_status };
}


/** The runtime this subtree runs against, or `null` outside a provider — so a
 *  surface mounted anywhere else knows to bring its own rather than reading one
 *  that is not there. A context rather than a flag: being under a provider and
 *  having that provider's pair are the same fact, and two ways of saying it can
 *  disagree. */
const RuntimeContext = createContext<ChatRuntime | null>(null);

/** The runtime from context. React consumers should prefer this over the
 *  module getter — it cannot be read outside the subtree that owns it. */
export function useChatRuntime(): ChatRuntime | null {
  return useContext(RuntimeContext);
}

export function useChatRuntimeInstalled(): boolean {
  return useContext(RuntimeContext) !== null;
}

/** The install itself, for a subtree. The route form below is what the app
 *  mounts; a chat surface mounted on its own wraps itself in this so it is
 *  never a page with nothing behind it. */
export function ChatRuntimeProvider({ children }: { children: ReactNode }): ReactElement | null {
  const me = useCurrentUser();
  // The browser names the user and the org it is acting in, so what the chat
  // remembers in this browser is filed under both.
  const account: ChatAccount = {
    email: me.data?.email ?? null,
    webAppUrl: null,
    userId: me.data?.id ?? null,
    orgId: me.data?.org_team_id ?? null,
  };
  // One source and one host for the life of the chat, with the account READ at
  // call time rather than captured.
  //
  // The account resolves a moment after the page mounts, and rebuilding the
  // runtime for it handed the chat a second source that had never read the
  // chat — while the surface, which is not remounted for it, kept its
  // subscription on the first. Every later read then came off a fold holding
  // only what this tab had sent since: the reader's next message appeared
  // twice, with the conversation above it gone, until they reloaded.
  const accountRef = useRef(account);
  accountRef.current = account;
  // One pair for the life of THIS provider instance, and one owner token to
  // stamp it with. A ref rather than a memo: a memo is a cache React may drop,
  // and a dropped source is a second subscription that has never read the chat.
  const ownedRef = useRef<{ owner: object; runtime: ChatRuntime } | null>(null);
  ownedRef.current ??= {
    owner: {},
    runtime: {
      source: new CloudDataSource({
        account: () => chatAccountScope(accountRef.current),
      }),
      host: createBrowserChatHost({
        account: () => accountRef.current,
      }),
    },
  };
  const { owner, runtime } = ownedRef.current;
  // Nothing is installed from render. A render is not a commit: React runs
  // renders it goes on to throw away — an interrupted transition (which is how
  // react-router navigates), a retried suspense, StrictMode's own second
  // invocation, which gets a fresh ref and so builds a second pair. An install
  // from one of those lands in the slot under a DIFFERENT owner than the
  // instance that is actually mounted, and from then on the mounted instance's
  // release can only no-op: the discarded pair stays installed until something
  // else overwrites it. So the install happens once, in the commit, and the
  // children are not rendered until it has.
  const [held, setHeld] = useState(false);
  useLayoutEffect(() => {
    installChatRuntime(runtime, owner);
    setHeld(true);
    return () => releaseChatRuntime(owner);
  }, [owner, runtime]);
  // A surface reads the runtime in its own render — a hook body calling
  // `chatData()` — so it may not render before the install. `useLayoutEffect`
  // plus this flag is what makes that safe AND invisible: React finishes the
  // re-render it triggers before the browser paints, so the chat area is never
  // a blank frame. A passive effect here would show one.
  //
  // The flag is not reset on teardown, and must not be: React tears a subtree's
  // effects down and builds them straight back without re-rendering it — a
  // StrictMode remount, a Suspense re-reveal, an error-boundary reset — and a
  // reset would unmount every child for a frame each time. Children stay
  // mounted through that window, which is why `releaseChatRuntime` holds the
  // release back rather than applying it where it is asked for.
  if (!held) return null;
  return <RuntimeContext.Provider value={runtime}>{children}</RuntimeContext.Provider>;
}

/** The route every chat surface sits under: the chat itself and everything
 *  stacked over it, sharing one installation. */
export function ChatRuntimeLayout(): ReactElement {
  return (
    <ChatRuntimeProvider>
      <Outlet />
    </ChatRuntimeProvider>
  );
}
