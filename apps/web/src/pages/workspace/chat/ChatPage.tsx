// The chat, in a browser tab — a workspace rather than a page.
//
// This is the SAME chat the extension renders — `ChatSurface` and everything
// under it is shared verbatim. What the browser adds around it is an IDE's
// arrangement: a rail of the org's chats on one side, the chat's own files on
// the other, and a gutter between each pair the reader drags to give whichever
// one they are working in the room it needs.
//
// The three tiers of layout state are kept apart on purpose. Which tabs are
// open belongs to the reader wherever they sign in, so it lives on the server
// (the pane reads it). How wide the columns are belongs to the screen in front
// of them, so it lives in this browser. And below 900 px there are no columns
// at all: a drawer for the rail and one switch between the chat and its files,
// with nothing written down, because a width measured on a phone is not a
// preference the reader expressed about their desk.
//
// The runtime this page reads through is installed by the route above it
// (`ChatRuntimeLayout`), which every surface stacked over the chat shares.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
} from "react";
import {
  Link,
  Navigate,
  Outlet,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";

import {
  Button,
  EmptyState,
  SegmentedControl,
  SidePanel,
  SplitPane,
  statusIsResting,
  statusNeedsSaying,
  useMediaQuery,
  type PaneSpec,
} from "@alkera/ui";

import { useCurrentUser } from "../../../api/auth";
import { keys } from "../../../api/keys";
import { useStatusRecheck } from "../../../api/status";
import { userScope, type AccountScope } from "../../../lib/accountScope";
import { ApiError } from "../../../api/errors";
import { NotFoundPage } from "../../NotFoundPage";
import {
  useChat,
  useChats,
  useCreateChat,
  useWakeOnOpen,
  useMarkChatUnread,
  useMarkReadOnView,
  useMarkWorkspaceRead,
  type ChatSessionRead,
} from "../../../api/chats";
import { useDrive, useEnsurePlaces, useItem } from "../../../api/files";
import { renameRefusal, useRenameChat } from "../../../api/objects";
import { useMainWorkspace, type WorkspaceRead } from "../../../api/workspaces";
import { usePublicConfig } from "../../../api/config";
import { getRealtimeClient } from "../../../api/realtime/client";
import { TopbarActions } from "../../../app/Topbar";

import { chatFilesNodeOf } from "@/lib/files/chatFolder";
import { SaveAsTemplateDialog } from "../files/SaveAsTemplateDialog";
import { ShareDialog } from "../files/ShareDialog";

import { ChatSurface } from "./ChatSurface";
import { ChatPresence } from "./ChatPresence";
import type { ChatRailEntry } from "./ChatRailRow";
import { chatTitle } from "@/lib/chatTitle";
import { chatFilesRoot, workspaceTitle } from "../workspaces/chatWorkspace";
import { useWorkspaceViewers } from "../workspaces/useWorkspacePresence";
import { NEW_CHAT_WORKSPACE_PARAM, useWorkspaceRail } from "../workspaces/useWorkspaceRail";
import { ownedBy } from "../workspaces/railGroups";
import { NewChatPlace, NewChatRefused } from "../workspaces/SleepNote";
import { useRefreshOnAccessLoss } from "../workspaces/useRefreshOnAccessLoss";
import { useRefreshFilesOnWake } from "../workspaces/useRefreshFilesOnWake";
import { WorkspaceCrumb } from "../workspaces/WorkspaceCrumb";
import { WakeMachinePrompt, WorkspaceMachineChip } from "../workspaces/WorkspaceMachine";
import { PinnedMachineBanner } from "./PinnedMachineBanner";
import { WorkspacePage } from "../workspaces/WorkspacePage";
import { WorkspaceRail } from "../workspaces/WorkspaceRail";
import "../workspaces/workspaces.css";
import { MACHINE_COPY, MachineBanner, machineIsReady } from "./MachineBanner";
import { useOpenFileFromLink } from "./openFileLink";
import { ChatSidePane } from "./workspace/ChatSidePane";
import {
  createLayoutWriter,
  PANE_BOUNDS,
  RAIL_BOUNDS,
  readLayout,
  type ChatLayout,
  type LayoutWriter,
} from "./workspace/layoutStorage";
import { useWorkingChats } from "./RailPresence";
import { RenameRefusedError } from "./RailRename";
import { useSpareWarmer } from "./useSpareWarmer";
import { copyLabel, useChatDuplicate } from "./useChatCopyAction";
import { useChatDeleteConfirm, useDeleteOneChat } from "./useDeleteChatAction";
import { useSaveAsTemplateAction } from "./useSaveAsTemplateAction";
import {
  ChatRuntimeProvider,
  useChatRuntimeInstalled,
  useMachineStatus,
} from "./ChatRuntimeLayout";
import { forgetLastChat, readLastChat, rememberLastChat } from "./lastOpenChat";
import { useSurfaceKey } from "./useSurfaceKey";
import "./chat-surface.css";
import "./chat-page.css";
import { granted } from "@/lib/capabilities";

/** Whether the chat read said, definitively, that there is no such chat.
 *
 *  Only a 404 says it. A chat that is deleted, or in another org, is the same
 *  opaque 404 by design, and either way there is nothing at that id for this
 *  reader to come back to — so the id can be dropped and the reader sent to an
 *  empty composer. A read still in flight, a 500 and a browser that is offline
 *  say nothing of the kind: they are a chat that did not load, which is the
 *  chat page's own problem to show, not a reason to walk away from the id.
 */
export function chatIsGone(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** What a reader is told when the chat read itself is refused.
 *
 *  A 403 is told apart from a 404 where the server distinguishes them: the
 *  first says the chat exists and is closed to this reader — which is worth
 *  saying, because asking the owner for access is a thing the reader can go and
 *  do. The second says nothing here can confirm it exists at all, and is
 *  handled by {@link chatIsGone} instead: a dead end with a Back key is a worse
 *  answer than simply being in a new chat.
 */
export function unreadableChat(error: unknown): { title: string; body: string } | null {
  const status = error instanceof ApiError ? error.status : null;
  if (status === 403) {
    return {
      title: "You don't have access to this chat",
      body: "Ask the owner to share it with you.",
    };
  }
  return null;
}

/** What a reader who may not send is told, in place of the composer.
 *
 *  Which rung they hold never enters into it: the composer asks the capability
 *  the send is decided against, so every read-only rung reads the same - this
 *  chat is theirs to follow, not to write in. The composer stays where it is -
 *  disabled, carrying this - rather than vanishing, which is how every other
 *  unavailable state on this page reads. */
/** What a chat's creator is told when they may only read it: the workspace it
 *  is in is no longer shared with them at edit. */
export const READ_ONLY_OWN_CHAT = "You no longer have edit access to this chat's workspace.";

export const READ_ONLY_CHAT =
  "You can read this chat. Ask the owner for edit access to send messages.";

/** Why a reader who may not send may not change the permission mode either:
 *  the stance decides what the agent does next, which is speaking in the chat. */
export const MODE_LOCKED_READ_ONLY = "You can view this chat, not change its permission mode.";

/** The path that means "no chat open yet".
 *
 *  `/chat` is the nav's one Chat leaf, and it resolves to whatever chat this
 *  account was last reading — that is the whole point of remembering one. So
 *  the empty composer needs a path of its own that says it plainly; without it,
 *  starting a chat from scratch stops being reachable the moment anything is
 *  remembered. */
export const NEW_CHAT_PATH = "/chat/new";

/** The dead end an id with no chat behind it lands on.
 *
 *  What it says is deliberately about the CHAT and not the address: the reader
 *  followed a link to a conversation, and "this page doesn't exist" answers a
 *  question they did not ask. Which of the two things happened is never said —
 *  a chat in another org answers the same opaque 404 a deleted one does, and
 *  naming one of them would make this page a way to ask whether an id exists
 *  somewhere the reader cannot see. */
export const CHAT_GONE_TITLE = "This chat doesn't exist";
export const CHAT_GONE_BODY = "It was deleted, or it was never shared with you.";

/** The way out of the dead end, and the one key on the empty composer's own
 *  header. The rail stays where it is beside both. */
export const NEW_CHAT = "New chat";

/** The key beside New chat. It leads to the folder templates are filed in
 *  rather than to a picker: a template is a folder, and the browser that lists
 *  folders is the one place it can also be renamed, shared or read. */
export const NEW_FROM_TEMPLATE = "New from template";

/** The shape of a chat id, decided in the browser.
 *
 *  Whatever is in the address bar arrives here as the route's parameter, and an
 *  id the server cannot even parse comes back as its validator's own words
 *  ("Input should be a valid UUID, invalid character: found `n` at 1") — text
 *  nobody wrote for a reader, on a page that had already drawn a composer for a
 *  chat that was never going to exist. So the shape is decided before the read:
 *  an address that cannot name a chat is an address this app does not have. */
const CHAT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Whether `value` could name a chat at all. */
export function isChatId(value: string): boolean {
  return CHAT_ID.test(value);
}

/** The element behind `/chat/:chatId`.
 *
 *  It stands in front of the page rather than inside it so a malformed id costs
 *  no request at all: the rail, the drive and the account are reads the chat
 *  page makes on mount, and none of them are owed to an address that is not a
 *  chat's. */
export function ChatRoute(): ReactElement {
  const { chatId } = useParams<{ chatId: string }>();
  if (chatId !== undefined && !isChatId(chatId)) return <NotFoundPage />;
  return <ChatPage />;
}

/** The element behind `/workspaces/:workspaceId`: the chat page with the
 *  workspace in the middle, behind the same id check a chat's address gets. */
export function WorkspaceRoute(): ReactElement {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  if (workspaceId !== undefined && !isChatId(workspaceId)) return <NotFoundPage />;
  return <ChatPage />;
}

/** The dead end a chat id with nothing behind it lands on, wherever under the
 *  chat it was followed to. */
export function ChatGone(): ReactElement {
  return (
    <EmptyState
      title={CHAT_GONE_TITLE}
      body={CHAT_GONE_BODY}
      action={
        <Link className="alk-btn" to={NEW_CHAT_PATH}>
          {NEW_CHAT}
        </Link>
      }
    />
  );
}

/** The dead end for a chat this reader may not open. */
function ChatUnreadable({ title, body }: { title: string; body: string }): ReactElement {
  return (
    <EmptyState
      tone="alert"
      title={title}
      body={body}
      action={
        <Link className="alk-btn" to={NEW_CHAT_PATH}>
          Back to chats
        </Link>
      }
    />
  );
}

/** The layout in front of a chat's stacked pages — its results, one result, a
 *  plan, a compaction summary.
 *
 *  Each of those pages reads only its own slice of the chat, and a slice of a
 *  chat that is not there came back as that page's own empty or failed state:
 *  "No results yet" over an id nothing answers for, which reads as a chat that
 *  exists and has nothing in it. So the chat row is read here, in front of all
 *  of them, and settles the question the way the chat page does — an address
 *  that cannot name a chat is an address this app does not have, and a chat
 *  the server has no record of (deleted, or in another org: one opaque 404) is
 *  one dead end with a way out. A read still in flight or failing for any other
 *  reason leaves the page to draw its own state. Mounted only by the portal's
 *  routes: the editor opens the same pages over its own source, which has no
 *  chat row to read. */
export function ChatStackedRoute(): ReactElement {
  const { chatId } = useParams<{ chatId: string }>();
  const valid = chatId !== undefined && isChatId(chatId);
  const chat = useChat(valid ? chatId : undefined);
  if (!valid) return <NotFoundPage />;
  // The first refusal decides, as on the chat page: the retry ladder is for a
  // read that might yet answer, and a 404 already has.
  const refusal = chat.error ?? chat.failureReason;
  if (chatIsGone(refusal)) return <ChatGone />;
  const unreadable = unreadableChat(refusal);
  if (unreadable) return <ChatUnreadable {...unreadable} />;
  return <Outlet />;
}

/** Below this the columns go away entirely: there is no width left to split
 *  three ways, so the rail becomes a drawer and the chat and its files take
 *  turns. The same breakpoint the shell's own navigation uses. */
const NARROW_QUERY = "(max-width: 900px)";

/** Where the files pane is worth opening unasked. Narrower than this and a
 *  reader who has never touched the layout gets the whole width for the
 *  conversation; they can still open the pane, and then it is remembered. */
const WIDE_QUERY = "(min-width: 1280px)";

/** The share of the window the files pane may take. Expressed to the split view
 *  as a percentage of the container, and resolved to a number here only to
 *  clamp what this browser remembered. */
const PANE_SHARE = 0.6;

/** The narrowest the conversation column gets beside the rail and the files
 *  pane. The transcript, the permission card and the composer all fold to fit
 *  well below it (container queries), so it is set by what still reads
 *  comfortably, not by the widest row. */
export const CONVERSATION_MIN = 440;

/** How the files pane is named to a screen reader — on the gutter that resizes
 *  it and on the key that brings it back. */
export const WORKSPACE_PANE_LABEL = "Files panel";

/** The one column's two states, below the breakpoint. */
const VIEW_OPTIONS = [
  { key: "chat", label: "Chat" },
  { key: "files", label: "Files" },
] as const;

function paneCeiling(): number {
  const width = typeof window === "undefined" ? 0 : window.innerWidth;
  if (width <= 0) return PANE_BOUNDS.max;
  return Math.max(PANE_BOUNDS.min, Math.min(PANE_BOUNDS.max, Math.round(width * PANE_SHARE)));
}

interface ChatLayoutState {
  layout: ChatLayout;
  resize(paneId: string, px: number): void;
  toggle(paneId: string, collapsed: boolean): void;
}

/**
 * How wide the columns are on THIS machine, for THIS account — and, for the
 * files pane, for THIS chat.
 *
 * Read once the account is known — the first render happens before `/auth/me`
 * has answered, and a layout stored under one account is not another's. Every
 * read and write is clamped to the ranges the split view can actually draw, so
 * a width left behind by a wider monitor, a different build, or a hand-edited
 * value can never lay the page out wrong.
 *
 * The rail is the same width whichever chat is open, because it lists all of
 * them. The files pane is not: one conversation is prose and the next is three
 * files side by side, so moving to another chat re-reads the pane, and the one
 * being left behind keeps the width it was given.
 */
function useChatLayout(
  scope: AccountScope | null,
  chatId: string | null,
  narrow: boolean,
  wide: boolean,
): ChatLayoutState {
  const options = useMemo(
    () => ({ pane: { max: paneCeiling() }, paneCollapsedByDefault: !wide, chatId }),
    [wide, chatId],
  );
  const [layout, setLayout] = useState<ChatLayout>(() => readLayout(null, options));
  const latest = useRef(layout);
  latest.current = layout;
  // One place to put a width, for the life of the page. A drag hands it a width
  // per pointer frame and it writes the one the drag stopped on, so the columns
  // redraw under the cursor without paying a serialize-and-store on each frame.
  const writerRef = useRef<LayoutWriter | null>(null);
  writerRef.current ??= createLayoutWriter();
  const writer = writerRef.current;
  // A width chosen a moment before leaving the page is still a width the reader
  // chose, so what is owed is written on the way out rather than dropped.
  useEffect(() => () => writer.flush(), [writer]);
  /** Whether the reader has arranged the page themselves this session. */
  const touched = useRef(false);
  // Narrow never reads and never writes: there are no columns to remember.
  const read = useRef<string | null>(null);
  // Before the frame, not after it. The account arrives from a read, so the
  // arrangement it unlocks is applied a pass later than the render that learned
  // the account — and a pass later is a painted frame in which a rail the reader
  // folded away is open again. Reading here puts the columns where they belong
  // in the same commit, ahead of everything else that pass does.
  useLayoutEffect(() => {
    if (narrow || !scope) return;
    const token = `${scope.userId}\u0000${scope.orgId}\u0000${chatId ?? ""}`;
    if (read.current === token) return;
    const first = read.current === null;
    // What the chat being left behind is owed is written before the next one is
    // read, or a fast move between two chats would file one's width under the
    // other.
    writer.flush();
    read.current = token;
    // The account is only known once `/auth/me` has answered, and the page is
    // already on screen and draggable by then. A reader who moved a gutter in
    // that window has stated the newer fact, so it is written under the account
    // rather than replaced by what the account remembered.
    if (first && touched.current) {
      writer.remember(scope, latest.current, options);
      return;
    }
    const stored = readLayout(scope, options);
    // Moving between chats re-reads the pane and nothing else: the rail lists
    // every chat, so its width was never this chat's to change.
    const next = first ? stored : { ...stored, rail: latest.current.rail };
    latest.current = next;
    setLayout(next);
  }, [chatId, narrow, options, scope, writer]);

  /** One change to one pane, remembered.
   *
   *  A drag lands many of these between two renders, so the change is composed
   *  onto the ref rather than onto the state a render last handed out — reading
   *  the state would make every move after the first in a frame start from the
   *  same width. */
  const apply = useCallback(
    (paneId: string, part: Partial<ChatLayout["rail"]>): void => {
      touched.current = true;
      const current = latest.current;
      const next: ChatLayout =
        paneId === "rail"
          ? { ...current, rail: { ...current.rail, ...part } }
          : { ...current, pane: { ...current.pane, ...part } };
      latest.current = next;
      setLayout(next);
      if (!narrow) writer.remember(scope, next, options);
    },
    [narrow, options, scope, writer],
  );

  const resize = useCallback(
    (paneId: string, px: number): void => apply(paneId, { width: px }),
    [apply],
  );
  const toggle = useCallback(
    (paneId: string, collapsed: boolean): void => apply(paneId, { collapsed }),
    [apply],
  );

  return { layout, resize, toggle };
}

/** The chat page, with the runtime it reads through guaranteed to be
 *  installed: the app's routes install it one level up, on the route this page
 *  shares with every surface stacked over it, and a page mounted on its own
 *  brings its own rather than being a surface with nothing behind it. */
export function ChatPage(): ReactElement {
  const provided = useChatRuntimeInstalled();
  return provided ? (
    <ChatPageBody />
  ) : (
    <ChatRuntimeProvider>
      <ChatPageBody />
    </ChatRuntimeProvider>
  );
}

function ChatPageBody(): ReactElement {
  const { chatId, workspaceId } = useParams<{ chatId: string; workspaceId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const me = useCurrentUser();
  const chats = useChats();
  const createChat = useCreateChat();
  const [searchParams, setSearchParams] = useSearchParams();
  // Where a new chat would be made: the workspace the composer is aimed at,
  // or the reader's Main where chats with no place named land. Its machine is
  // the one the composer waits on (a pinned workspace runs on its own).
  const multiChatOn = usePublicConfig().data?.workspaces_multi_chat === true;
  const aimedWorkspaceId = chatId === undefined ? searchParams.get(NEW_CHAT_WORKSPACE_PARAM) : null;
  const mainForNew = useMainWorkspace({ enabled: chatId === undefined && aimedWorkspaceId === null && multiChatOn });
  const { status, fact } = useMachineStatus(
    chatId,
    aimedWorkspaceId ?? (multiChatOn ? (mainForNew.data?.id ?? null) : null),
  );
  // The chat row is the read every other control on this page stands on, so a
  // refusal of it replaces the page rather than decorating it.
  const chat = useChat(chatId);
  // The read's answer, which is not the same as `error`: react-query publishes
  // that only once the retry ladder is exhausted, so a refusal the server gave
  // at once sat behind seven seconds of spinner — the chat shell drawn around a
  // chat that was already known not to be there. A refusal is an answer, so the
  // first one decides; a flake still gets its retries, it just does not get to
  // draw a composer in the meantime.
  const refusal = chat.error ?? chat.failureReason;
  const unreadable = chatId ? unreadableChat(refusal) : null;
  const gone = chatId ? chatIsGone(refusal) : false;
  // Which chat this account was last reading, and whether this render is the
  // one that should resolve to it. `/chat` is the nav's leaf, so it does;
  // `/chat/new` is the reader saying they want none, so it never does.
  const userId = me.data?.id ?? null;
  // What this browser remembers for the reader is filed under them AND the org
  // they are acting in, so switching orgs never resumes another org's chat.
  const orgId = me.data?.org_team_id ?? null;
  const scope = useMemo(
    () => userScope(userId ? { id: userId, org_team_id: orgId } : null),
    [userId, orgId],
  );
  // While this page is open, one chat is kept warmed ahead for this reader so
  // the empty composer's first send does not wait for the box to spawn.
  useSpareWarmer(Boolean(userId));
  const resume = chatId === undefined && location.pathname === "/chat" ? readLastChat(scope) : null;
  // A chat is remembered once it has actually been read: an id that 404s, or
  // one belonging to an org this account cannot see, never becomes the place
  // the nav leads to.
  const readable = chat.isSuccess;
  useEffect(() => {
    if (!chatId || !readable) return;
    rememberLastChat(scope, chatId);
  }, [scope, chatId, readable]);
  // And it is dropped the moment the chat refuses to open, so a remembered chat
  // that has been deleted or unshared costs this reader ONE bounce, never a
  // door that keeps leading back to a dead transcript.
  const chatRefused = gone || unreadable !== null;
  useEffect(() => {
    if (!chatId || !chatRefused) return;
    forgetLastChat(scope, chatId);
  }, [scope, chatId, chatRefused]);
  // The rung this reader holds on the chat's node IS what a send is decided
  // against, so the composer asks the same question the server will. A chat
  // with no node (Files off, or one created before the drive existed) has no
  // rung to read and is left alone: the send path's own refusal covers it.
  const drive = useDrive();
  const driveId = drive.data?.id;
  const node = useItem(driveId, chat.data?.files_node_id ?? undefined);
  // A link that names a file (the Files page sending a notebook to its
  // editor) opens it in this chat's pane.
  useOpenFileFromLink(chatId, driveId);
  const readOnly = node.data?.capabilities?.can_write === false;
  // The pane is rooted where the chat's agent works: the workspace's shared
  // `files/` tree when the chat read names one, else the chat's WORKING
  // DIRECTORY, which the server names on the chat folder's facet. A chat from
  // before there was one keeps its folder as the root. Either way there is
  // no pane at all without a readable chat that has a folder: an empty composer
  // has no files, and a chat this reader was refused has none to show.
  const workspaceRoot = chat.isSuccess
    ? chatFilesRoot(chat.data, chatFilesNodeOf(node.data))
    : null;
  // A chat is renamed by its own name in the header, and nowhere else: the rail
  // lists names, it does not edit them. The seam is wired only for the chat
  // that is OPEN and only for a reader who may write it — that is the one chat
  // whose rung this page has read, and an editor the object policy is certain
  // to refuse is worse than a title that is simply not pressable.
  const renameChat = useRenameChat();
  // Any chat in the rail can be renamed from its own row, not only the one that
  // is open: the row's menu is where a reader looks for it, and a rename that
  // meant first opening the chat is a rename nobody finds.
  const rename = async (id: string, title: string): Promise<void> => {
    try {
      await renameChat.mutateAsync({ chatId: id, title });
    } catch (error) {
      throw new RenameRefusedError(renameRefusal(error));
    }
  };
  // Deleting from a row: the question the header asks, in the same dialog, and
  // an empty composer only when the chat that went is the one on screen —
  // deleting a row from under an open chat must leave the reader where they
  // were.
  const removeOneChat = useDeleteOneChat();
  const deleteConfirm = useChatDeleteConfirm((id) => {
    // Leaving, and the forgetting, happen on the confirm rather than in the
    // mutation's callback, for the reason the header's own action carries: a
    // callsite callback runs only once the cache's invalidation has been
    // awaited, and that awaits a refetch of the deleted chat's own queries,
    // which 404.
    if (id === chatId) navigate(NEW_CHAT_PATH, { replace: true });
    removeOneChat(id);
  });
  // One naming dialog for the whole rail, so a copy asked for from a row and a
  // copy asked for from the header are the same question.
  const duplicate = useChatDuplicate(drive.data?.id);
  // And one of each for the rail's other two dialogs, for the same reason: a
  // row that is re-rendered, or scrolled past, must not take the question with
  // it.
  const saveTemplate = useSaveAsTemplateAction(drive.data?.id);
  const [sharing, setSharing] = useState<{ nodeId: string; name: string } | null>(null);
  // "New from template" opens the folder templates are filed in. The folder is
  // made on the way if this reader has never saved one — asking the server for
  // the id and for its creation in one request, rather than reading a `null`
  // and landing somebody on a listing that is not there.
  const ensurePlaces = useEnsurePlaces();
  // Pressed twice before the first request answers: the mutation's own
  // `isPending` flips on the next render, which two presses in one tick do not
  // wait for, so the guard is a ref set at the press and cleared at the answer.
  // Without it a double-press makes the folder twice over.
  const openingTemplates = useRef(false);
  const openTemplates = (): void => {
    const driveId = drive.data?.id;
    if (!driveId || openingTemplates.current) return;
    openingTemplates.current = true;
    ensurePlaces.mutate(
      { driveId, places: ["chatTemplates"] },
      {
        onSettled: () => {
          openingTemplates.current = false;
        },
        onSuccess: (places) => {
          if (places.chatTemplatesId) navigate(`/files/${places.chatTemplatesId}`);
        },
      },
    );
  };
  // Offered once the answer is in, never while it is loading or failing: a
  // reader who may only view was shown Rename for as long as the rung read
  // had not answered. A chat with a node asks the node's rung; one without
  // (Files off) asks the chat read's own capability.
  const mayRename = node.data
    ? node.data.capabilities?.can_write === true
    : chat.isSuccess && !chat.data.files_node_id && chat.data.can_send === true;
  const onRenameTitle =
    chatId && mayRename
      ? async (title: string): Promise<void> => {
          try {
            await renameChat.mutateAsync({ chatId, title });
          } catch (error) {
            // The header reads the rejection's own message out as the refusal,
            // so the reason the write was refused reaches the reader in the
            // server's words rather than as a title that silently sprang back.
            throw new RenameRefusedError(renameRefusal(error));
          }
        }
      : undefined;
  // An UNKNOWN status is not a verdict. Both reads are in flight for a moment
  // on every navigation, and reading that as "no workspace" disabled the
  // composer and told the reader their org has no machine — on a chat whose
  // box is running fine. The composer is only taken away once a status is
  // known and is not ready.
  const ready = status === undefined || machineIsReady(status);
  // The surface stays the same instance across the hop from the composer a
  // chat was started on, and remounts on every other change of chat.
  const surfaceKey = useSurfaceKey(chatId);

  // How the page is arranged, and who decides. Both queries are read on every
  // render rather than once: a window dragged across the breakpoint has to
  // rearrange, not wait for a reload.
  const narrow = useMediaQuery(NARROW_QUERY);
  const wide = useMediaQuery(WIDE_QUERY);
  const { layout, resize, toggle } = useChatLayout(scope, chatId ?? null, narrow, wide);
  // Narrow only: the rail's drawer, and which of the two things the one column
  // is showing.
  const [drawer, setDrawer] = useState(false);
  const [view, setView] = useState<"chat" | "files">("chat");

  const entryOf = useCallback(
    (row: ChatSessionRead): ChatRailEntry => ({
      id: row.id,
      title: chatTitle(row),
      nodeId: driveId && row.files_node_id ? row.files_node_id : null,
      owner: Boolean(userId) && row.owner_user_id === userId,
      // Fail closed: a row that does not say may not delete, the same reading
      // the chat header makes of it.
      canDelete: granted(row.can_delete),
    }),
    [driveId, userId],
  );

  // Which chats sit together, the workspace around the open chat, and the
  // workspace writes with their dialogs.
  const chatRows = useMemo(() => chats.data?.items ?? [], [chats.data?.items]);
  const rail = useWorkspaceRail({
    chats: chatRows,
    userId,
    chatId,
    workspaceId,
    newChatPath: NEW_CHAT_PATH,
  });
  const { holder } = rail;
  // The empty composer may be aimed at a workspace (`/chat/new?workspace=<id>`):
  // its first send makes the chat there. Only a workspace this reader may add a
  // chat to is honoured; anything else is the plain empty composer.
  const aimedAt = chatId === undefined ? searchParams.get(NEW_CHAT_WORKSPACE_PARAM) : null;
  const newChatWorkspace =
    aimedAt !== null
      ? (rail.listed?.find((w) => w.id === aimedAt && w.can_add_chat) ?? null)
      : null;
  // Where the empty composer's chat will be made, said above it: the aimed
  // workspace, or the reader's main one where a chat with no place named
  // lands there.
  const newChatPlace =
    newChatWorkspace ??
    (rail.multiChat
      ? (rail.listed?.find((w) => w.kind === "main" && userId !== null && ownedBy(w, userId)) ??
        null)
      : null);
  // An aim the list has answered and cannot honour: a workspace this reader
  // may not open, or may not add a chat to. Said, never a silent fallback.
  const aimRefused = aimedAt !== null && rail.listed !== undefined && newChatWorkspace === null;
  // The chat's state as the server reports it, for the line that says a
  // sleeping chat wakes on a send and for the Files pane to catch up on wake.
  useRefreshFilesOnWake(chat.data?.session_state ?? null);
  // A wait the server said would read differently with time alone (a message
  // nothing started on, a turn whose worker went quiet) is read again then.
  useStatusRecheck([chat.data?.status], keys.chats.one(chatId));
  useStatusRecheck(
    chatRows.map((row) => row.status),
    keys.chats.all,
  );
  useStatusRecheck((rail.listed ?? []).map((row) => row.status), keys.workspaces.all);
  // Opening the chat wakes it, so a notebook in its workspace runs before
  // anything is sent. The server's `can_send` decides whether to ask, and a
  // chat it says is already awake is not asked for.
  const opened = useWakeOnOpen(chatId, chat.data?.can_send, chat.data?.session_state);
  // What the open chat shows is read: the reader's own mark follows the
  // transcript as the server writes it.
  useMarkReadOnView(
    chatId,
    chat.data?.last_seq,
    chats.data?.items.find((row) => row.id === chatId)?.unread,
  );
  const markUnread = useMarkChatUnread();
  const markAllRead = useMarkWorkspaceRead();
  // Access taken away while the chat is open (its workspace unshared) shows
  // first in the workspace list. The chat row and the drive reads are then
  // re-read at once, so every control follows the server's new answer
  // instead of the one the page opened with.
  const inWorkspace =
    chat.data?.workspace_id && rail.listed
      ? rail.listed.some((w) => w.id === chat.data?.workspace_id)
      : null;
  useRefreshOnAccessLoss(chatId, inWorkspace);
  const viewerChatIds = useMemo(
    () =>
      workspaceId
        ? chatRows.filter((row) => row.workspace_id === workspaceId).map((row) => row.id)
        : [],
    [chatRows, workspaceId],
  );
  // Being in a workspace is having its page or one of its chats open, so
  // both join its presence; only the page reads who else is there.
  const viewers = useWorkspaceViewers(
    workspaceId ?? holder?.workspace.id ?? newChatWorkspace?.id ?? null,
    viewerChatIds,
  );
  const currentWorkspace: WorkspaceRead | null =
    (workspaceId ? rail.listed?.find((w) => w.id === workspaceId) : undefined) ??
    holder?.workspace ??
    newChatWorkspace;

  // Installed one level up, on the route this page and every surface stacked
  // over it share, so a cold load of a plan or a compaction summary has the
  // same runtime this page does.
  const installed = useChatRuntimeInstalled();
  // Which chats are mid-turn, from the same words the transcript is gated on,
  // so the rail's light and the working line under the composer agree.
  const working = useWorkingChats(chatId, installed);

  const start = (sourceNodeId?: string): void => {
    createChat.mutate(
      { title: null, ...(sourceNodeId ? { sourceNodeId } : {}) },
      { onSuccess: (chat) => navigate(`/chat/${chat.id}`) },
    );
  };
  // Inside a workspace that holds several chats, the empty composer is aimed
  // at it, so the first send makes the chat there.
  const newChat = (): void => {
    if (currentWorkspace?.can_add_chat) rail.startIn(currentWorkspace);
    else navigate(NEW_CHAT_PATH);
  };
  const shareWorkspace = (workspace: WorkspaceRead): void => {
    if (workspace.files_node_id) {
      setSharing({ nodeId: workspace.files_node_id, name: workspaceTitle(workspace) });
    }
  };

  // "Start a chat from this report" in Files routes here with the folder's node
  // on the URL rather than creating the chat itself: the chat is opened by the
  // surface that owns chats, so the reader lands IN the new chat with the rail,
  // the composer and the machine banner already mounted. The parameter is
  // dropped the instant it is spent, so a refresh or a back-navigation cannot
  // open a second chat from the same report.
  useEffect(() => {
    const source = searchParams.get("source");
    if (!source || chatId !== undefined || createChat.isPending) return;
    setSearchParams(
      (held) => {
        held.delete("source");
        return held;
      },
      { replace: true },
    );
    start(source);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `start` is re-made every render; the effect is keyed on what it reads
  }, [searchParams, chatId]);

  // Resolved after the hooks, never before them: this page is the same
  // component instance on `/chat`, `/chat/new` and `/chat/<id>`, and an early
  // return would change how many hooks it runs between two of them.
  if (resume) return <Navigate to={`/chat/${resume}`} replace />;
  const railView = (
    <WorkspaceRail
      groups={rail.groups}
      currentChatId={chatId}
      currentWorkspaceId={workspaceId}
      working={working}
      empty={chats.isSuccess && chatRows.length === 0}
      onNewWorkspace={rail.multiChat ? rail.askNew : undefined}
      chat={{
        entryOf,
        copyLabel,
        onOpen: (entry) => {
          setDrawer(false);
          navigate(`/chat/${entry.id}`);
        },
        onCopy: (entry) =>
          entry.nodeId
            ? duplicate.ask({
                chatId: entry.id,
                nodeId: entry.nodeId,
                title: entry.title,
                owner: entry.owner,
              })
            : undefined,
        onSaveAsTemplate: (entry) =>
          entry.nodeId ? saveTemplate.ask({ nodeId: entry.nodeId }) : undefined,
        onShare: (entry) =>
          entry.nodeId ? setSharing({ nodeId: entry.nodeId, name: entry.title }) : undefined,
        onDelete: (entry) => deleteConfirm.ask(entry.id, entry.title),
        onRename: (entry, title) => rename(entry.id, title),
        onMarkUnread: (entry) => markUnread.mutate(entry.id),
      }}
      workspace={{
        onOpen: (workspace) => {
          setDrawer(false);
          navigate(`/workspaces/${workspace.id}`);
        },
        onNewChat: (workspace) => {
          setDrawer(false);
          rail.startIn(workspace);
        },
        onRename: rail.rename,
        onShare: shareWorkspace,
        onDelete: rail.askDelete,
        onMarkAllRead: (workspace) => markAllRead.mutate(workspace.id),
      }}
    />
  );

  const surface = (
    <div className="chat-page__surface">
      {/* A chat that is not there is not a chat: nothing under this id is
          drawn — no transcript, no composer, no files dock, and none of the
          chat-scoped reads the surface would start, because every one of them
          would ask about a chat the server has already said it has none of.
          What IS drawn is the way out. The rail keeps standing beside it, so a
          stale link costs the reader this chat and not the rest of them. */}
      {workspaceId && !chatId ? (
        <WorkspacePage
          workspaceId={workspaceId}
          rail={rail}
          chats={chatRows}
          viewers={viewers}
          userId={userId}
          onShare={shareWorkspace}
          newChatPath={NEW_CHAT_PATH}
          newChatLabel={NEW_CHAT}
        />
      ) : gone ? (
        <ChatGone />
      ) : unreadable ? (
        <ChatUnreadable {...unreadable} />
      ) : installed ? (
        <ChatSurface
          key={surfaceKey}
          chatId={chatId}
          // One shape whether or not a crumb is drawn: the faces must not
          // remount when the workspace list answers, or their roster join is
          // dropped and re-made against a channel the page already holds.
          presence={
            <>
              {holder && chatId ? <WorkspaceCrumb holder={holder} /> : null}
              {!chatId && aimRefused ? <NewChatRefused /> : null}
              {!chatId && !aimRefused && newChatPlace ? (
                <NewChatPlace workspace={newChatPlace} />
              ) : null}
              <ChatPresence chatId={chatId} />
            </>
          }
          machine={
            chatId && chat.data?.workspace_id ? (
              <WorkspaceMachineChip
                workspaceId={chat.data.workspace_id}
                workspaceVersion={
                  holder?.workspace.id === chat.data.workspace_id
                    ? holder.workspace.version
                    : undefined
                }
              />
            ) : null
          }
          notice={
            <>
              <PinnedMachineBanner
                workspaceId={chat.data?.workspace_id ?? undefined}
                fallback={<MachineBanner status={status} fact={fact} />}
              />
            </>
          }
          unavailable={!ready || readOnly}
          machineWaiting={statusNeedsSaying(fact) || status === "unreachable"}
          // What the sent message is waiting on, in the server's sentence. A
          // resting status is the read from before the send; the page's own
          // neutral line stands until the next read lands. A wake the open
          // asked for and the server refused says why while the chat waits.
          waitingLabel={
            statusIsResting(fact) ? (opened.refusal ?? undefined) : (fact?.sentence ?? undefined)
          }
          newChatWorkspaceId={newChatWorkspace?.id}
          workspaceId={chat.data?.workspace_id ?? undefined}
          // What the server said about THIS reader's right to send, straight
          // through: `undefined` (a chat row from before the field, or a read
          // that has not answered) leaves the dock as it was.
          canSend={chat.data?.can_send}
          // "Always" is offered only where the server says this reader may give
          // it: until the chat row answers, it is not.
          canAnswerAlways={chat.data?.can_answer_always === true}
          modeLockedReason={
            readOnly || chat.data?.can_send === false ? MODE_LOCKED_READ_ONLY : undefined
          }
          onRenameTitle={onRenameTitle}
          // The rung outranks the machine: a reader who may not send is not
          // waiting for a box to come up, and telling them so would have
          // them wait for something that will never change the answer.
          unavailableReason={
            readOnly
              ? // The chat's own creator reads a chat they may no longer write
                // because the workspace it is in stopped being shared with
                // them: asking "the owner" for access would send them to
                // themselves.
                chat.data && userId !== null && ownedBy(chat.data, userId)
                ? READ_ONLY_OWN_CHAT
                : READ_ONLY_CHAT
              : ready
                ? undefined
                : (fact?.sentence ?? MACHINE_COPY.unreachable.title)
          }
        />
      ) : null}
    </div>
  );

  const workspace =
    chatId && workspaceRoot && driveId ? (
      <div className="chat-page__workspace">
        <ChatSidePane
          chatId={chatId}
          driveId={driveId}
          rootNodeId={workspaceRoot}
          // The one fact the banner reads, handed to the Files tab so its rail
          // can never say "Live" while the banner says the workspace is not.
          machineReady={status === undefined ? undefined : machineIsReady(status)}
          liveSocket={getRealtimeClient()}
          onWake={opened.wake}
        />
      </div>
    ) : null;

  const dialogs = (
    <>
      <WakeMachinePrompt held={opened.unavailable} onWake={opened.wake} />
      {deleteConfirm.dialog}
      {rail.dialogs}
      {/* The rail's naming step before a copy is made. It sits out here rather
          than inside a row: a row that is re-rendered, or scrolled past, must
          not take the dialog with it. */}
      {duplicate.dialog}
      <SaveAsTemplateDialog {...saveTemplate.dialogProps} />
      {sharing && driveId ? (
        <ShareDialog
          driveId={driveId}
          nodeId={sharing.nodeId}
          // A chat's node is stored as `<uuid>.alkerachat`, so the dialog's own
          // read would title it with a raw id. The row knows what it is called.
          subjectName={sharing.name}
          // A chat or workspace runs on its owner's connections for everyone
          // it is shared with.
          sharesConnections
          open
          onClose={() => setSharing(null)}
        />
      ) : null}
    </>
  );

  const topbar = (
    <TopbarActions>
      <Button
        variant="secondary"
        fill="outline"
        disabled={!driveId || ensurePlaces.isPending}
        onClick={openTemplates}
      >
        {NEW_FROM_TEMPLATE}
      </Button>
      {/* A workspace's page carries its own New chat; one is enough. */}
      {workspaceId && !chatId ? null : (
        <Button
          variant="secondary"
          fill="outline"
          // The empty composer, not a chat row created up front: the chat is
          // born when the first message is sent, and this path is also the one
          // door that is NOT resolved to the remembered chat. Inside a
          // workspace that holds several chats, it is aimed there.
          disabled={createChat.isPending}
          onClick={newChat}
        >
          New chat
        </Button>
      )}
    </TopbarActions>
  );

  if (narrow) {
    // One column, and a switch that says which of the two things is in it. The
    // rail is a drawer because a third of a phone's width spent on a list of
    // other conversations is a third the conversation does not get.
    const showing = workspace && view === "files" ? workspace : surface;
    return (
      <div className="chat-page chat-page--stacked" data-content-fill="">
        {topbar}
        <div className="chat-page__bar">
          <Button variant="secondary" fill="outline" size="sm" onClick={() => setDrawer(true)}>
            Chats
          </Button>
          {workspace ? (
            <SegmentedControl
              label="View"
              size="sm"
              options={VIEW_OPTIONS}
              value={view}
              onChange={(key) => setView(key === "files" ? "files" : "chat")}
            />
          ) : null}
        </div>
        {showing}
        <SidePanel
          open={drawer}
          onClose={() => setDrawer(false)}
          mode="modal"
          anchor="viewport"
          side="left"
          title="Chats"
        >
          {railView}
        </SidePanel>
        {dialogs}
      </div>
    );
  }

  const panes: PaneSpec[] = [
    {
      id: "rail",
      min: RAIL_BOUNDS.min,
      max: RAIL_BOUNDS.max,
      size: layout.rail.width,
      collapsed: layout.rail.collapsed,
      collapsible: true,
      label: "Chats",
    },
    // Wide enough for a permission card's keys and the composer's chips; the
    // side panes give their width up first (see SplitPane). No wider, so the
    // files pane can be dragged wide enough to hold two editors side by side.
    { id: "chat", fill: true, min: CONVERSATION_MIN, label: "Chat" },
    ...(workspace
      ? [
          {
            id: "workspace",
            min: PANE_BOUNDS.min,
            max: `${Math.round(PANE_SHARE * 100)}%` as const,
            size: layout.pane.width,
            collapsed: layout.pane.collapsed,
            collapsible: true,
            label: WORKSPACE_PANE_LABEL,
          } satisfies PaneSpec,
        ]
      : []),
  ];

  return (
    <div className="chat-page" data-content-fill="">
      {topbar}
      <SplitPane label="Chat workspace" panes={panes} onResize={resize} onToggle={toggle}>
        {railView}
        {surface}
        {workspace}
      </SplitPane>
      {dialogs}
    </div>
  );
}
