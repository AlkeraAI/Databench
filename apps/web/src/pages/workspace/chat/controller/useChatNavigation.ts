// Where the chat can go: the stacked back-target, the subagent drill-in
// context, chat history, and every host-side open action.

import type {
  BlobReference,
  CompactionConversationPart,
  ConversationTurn,
  HistoryEntry,
  QuestionConversationPart,
  ResourceReference,
  SubagentConversationPart,
} from "@alkera/chat-model";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useLocation, useNavigate, type NavigateFunction } from "react-router-dom";
import { chatKeys } from "../chatKeys";
import { chatRoutes } from "../chatRoutes";
import { openPlanDocument, planDocumentOf } from "../planDocument";
import { chatData, chatHost, type Chat, type ChatDataSource } from "../data";
import { useTranscriptLookup } from "../useTranscriptLookup";

export function backTargetFromSearch(search: string): string | null {
  const raw = new URLSearchParams(search).get("backTo");
  return raw?.startsWith("/") ? raw : null;
}

/** The one segment under `/chat/` that is a surface rather than a chat id: the
 *  empty composer. Reading it as an id made every chat whose back pointer led
 *  there look like a chat opened out of a parent named "new". */
const NOT_A_CHAT_ID = "new";

export function chatIdFromPath(path: string | null): string | null {
  if (!path) return null;
  const pathname = path.split("?")[0] ?? path;
  const match = /^\/(?:editor\/)?chat\/([^/?#]+)/u.exec(pathname);
  if (!match) return null;
  const id = safeDecode(match[1]);
  return id === NOT_A_CHAT_ID ? null : id;
}

export function stackedTarget(path: string, sourceLocation: string): string {
  // A target can already carry a query (a blob names itself before its first
  // fetch), so the back pointer joins whatever is there rather than opening a
  // second `?` that would swallow it.
  return `${path}${path.includes("?") ? "&" : "?"}backTo=${encodeURIComponent(sourceLocation)}`;
}

export function findSubagentSource(
  turns: ConversationTurn[],
  childSessionId: string | null,
): SubagentConversationPart | null {
  if (!childSessionId) return null;
  for (const turn of turns) {
    const part = turn.parts.find(
      (candidate) => candidate.kind === "subagent" && candidate.childSessionId === childSessionId,
    );
    if (part?.kind === "subagent") return part;
  }
  return null;
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

// The fallback is the host's own file opener, which a shell without an editor
// does not have — so it is called only where it exists rather than resolved
// into a no-op that looks like it worked.
async function openResourceViaHost(resource: ResourceReference): Promise<void> {
  try {
    await chatHost().engine.request("resource.open", { resource });
  } catch {
    if (resource.path ?? resource.target)
      await chatHost().openFile?.(resource.path ?? resource.target);
  }
}

async function openDiffViaHost(resource: ResourceReference): Promise<void> {
  try {
    await chatHost().engine.request("diff.open", { resource });
  } catch {
    await chatHost().openFile?.(resource.path ?? resource.target);
  }
}

function historyOf(chats: Chat[] | undefined, chatId: string | null): HistoryEntry[] {
  const entries: HistoryEntry[] = [];
  for (const chat of chats ?? []) {
    if (chat.id !== chatId)
      entries.push({
        id: chat.id,
        title: chat.title,
        lastSnippet: undefined,
        updatedAt: undefined,
      });
  }
  return entries;
}

function openBlobTab(chatId: string, reference: BlobReference): void {
  const args = {
    chatId,
    handle: reference.handle,
    name: reference.name,
    refType: reference.refType,
    mime: reference.mime,
  };
  void chatHost().runCommand({ command: "alkera.openBlob", args });
}

export interface SubagentContext {
  parentTitle: string;
  subchatTitle?: string;
  subagentTitle: string;
  avatarSeed: string;
}

export interface ChatNavigation {
  backTarget: string;
  history: HistoryEntry[];
  /** Viewing a SUBAGENT's chat (drilled in from a parent) — read-only. */
  isSubagentView: boolean;
  subagentContext: SubagentContext | undefined;
  exitChat: () => void;
  refreshChatTitles: () => void;
  openResource: (resource: ResourceReference) => void;
  openDiff: (resource: ResourceReference) => void;
  openReference: (reference: BlobReference) => void;
  fetchBlob: (
    handle: string,
    offset: number,
    limit?: number,
  ) => ReturnType<ChatDataSource["fetchBlob"]>;
  openCompaction: (part: CompactionConversationPart) => void;
  openPlan: (part: QuestionConversationPart) => void;
  openSubagent: (childSessionId: string) => void;
  openLineageNode: (urn: string) => void;
  openKnowledgeItem: (itemId: string) => void;
  openResults: (() => void) | undefined;
  openHistoryChat: (chatId: string) => void;
  searchFiles: (query: string) => ReturnType<ChatDataSource["searchFiles"]>;
}

export function useChatNavigation({
  chatId,
  title,
  chats,
}: {
  chatId: string | null;
  title: string;
  chats: Chat[] | undefined;
}): ChatNavigation {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const sourceLocation = useMemo(
    () => `${location.pathname}${location.search}`,
    [location.pathname, location.search],
  );
  const backTarget = useMemo(
    () => backTargetFromSearch(location.search) ?? chatRoutes().home,
    [location.search],
  );
  const parentChatId = useMemo(() => chatIdFromPath(backTarget), [backTarget]);

  const subagent = useSubagentContext({ chatId, title, chats, parentChatId });
  const resources = useResourceOpeners({ chatId, navigate, sourceLocation });
  const routes = useRouteOpeners({ chatId, title, navigate, sourceLocation });

  const history = useMemo(() => historyOf(chats, chatId), [chats, chatId]);

  const exitChat = useCallback(() => navigate(backTarget), [navigate, backTarget]);
  const refreshChatTitles = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: chatKeys.chats() });
  }, [queryClient]);

  return {
    backTarget,
    history,
    isSubagentView: subagent.isSubagentView,
    subagentContext: subagent.subagentContext,
    exitChat,
    refreshChatTitles,
    ...resources,
    ...routes,
  };
}

/** The drilled-in subagent's naming, read from the spawn card in the PARENT
 *  chat's transcript (the chat list does not know a subagent's name). */
function useSubagentContext({
  chatId,
  title,
  chats,
  parentChatId,
}: {
  chatId: string | null;
  title: string;
  chats: Chat[] | undefined;
  parentChatId: string | null;
}): { isSubagentView: boolean; subagentContext: SubagentContext | undefined } {
  // Viewing a SUBAGENT's chat (drilled in from a parent) — read-only: you can't talk
  // to a subagent, so the composer area is removed. Same signal the parent-turns
  // query keys on (a stacked back-target pointing at a different chat).
  const isSubagentView = Boolean(parentChatId && chatId && parentChatId !== chatId);
  // The spawn card that named this subagent can sit far above the page the
  // parent chat opens on, so the read pages up until it holds the card — a
  // drill-in into a week-old subagent is titled, not left as a session id.
  const until = useCallback(
    (turns: ConversationTurn[]) => findSubagentSource(turns, chatId) !== null,
    [chatId],
  );
  const parentTurns = useTranscriptLookup(parentChatId, { enabled: isSubagentView, until }).turns;
  const currentChat = chatId ? chats?.find((chat) => chat.id === chatId) : undefined;
  const parentTitle = parentChatId
    ? (chats?.find((chat) => chat.id === parentChatId)?.title ?? parentChatId)
    : null;
  const subagentSource = useMemo(
    () => findSubagentSource(parentTurns, chatId),
    [chatId, parentTurns],
  );
  const subagentContext = useMemo(
    () =>
      parentChatId && chatId && parentTitle
        ? {
            parentTitle,
            subchatTitle: currentChat?.title,
            subagentTitle: subagentSource?.displayName ?? subagentSource?.name ?? title,
            avatarSeed: subagentSource?.avatarSeed ?? chatId,
          }
        : undefined,
    [chatId, currentChat?.title, parentChatId, parentTitle, subagentSource, title],
  );
  return { isSubagentView, subagentContext };
}

/** The chat's file, blob, and lineage actions. Host side effects, except the
 *  blob: the editor owns a tab per result, the portal a stacked page. */
function useResourceOpeners({
  chatId,
  navigate,
  sourceLocation,
}: {
  chatId: string | null;
  navigate: NavigateFunction;
  sourceLocation: string;
}) {
  const openResource = useCallback(
    (resource: ResourceReference): void => void openResourceViaHost(resource),
    [],
  );
  const openDiff = useCallback(
    (resource: ResourceReference): void => void openDiffViaHost(resource),
    [],
  );

  // Open a tool-result blob in its OWN editor tab (a WebviewPanel) — like
  // opening a file — rather than a subpage pushed onto the chat. The host
  // command owns the panel (one tab per handle; reveals if already open). The
  // blob descriptor rides in `args` so the tab titles itself + picks a renderer
  // before the first fetch.
  // …but only VS Code has editor tabs. In a browser the same surface opens as
  // the stacked page the chrome's other keys use, so a result chip is never a
  // command dispatched into nothing.
  const openReference = useCallback(
    (reference: BlobReference): void => {
      if (!chatId) return;
      if (chatHost().kind === "vscode") return openBlobTab(chatId, reference);
      navigate(stackedTarget(chatRoutes().blob(chatId, reference), sourceLocation));
    },
    [chatId, navigate, sourceLocation],
  );

  const openLineageNode = useCallback((urn: string): void => {
    void chatHost().runCommand({ command: "alkera.openLineage", args: { focus: urn } });
  }, []);
  const openKnowledgeItem = useCallback((itemId: string): void => {
    void chatHost().runCommand({ command: "alkera.openContext", args: { focus: itemId } });
  }, []);
  const fetchBlob = useCallback(
    (handle: string, offset: number, limit?: number) => chatData().fetchBlob(handle, offset, limit),
    [],
  );
  const searchFiles = useCallback((query: string) => chatData().searchFiles(query), []);

  return {
    openResource,
    openDiff,
    openReference,
    openLineageNode,
    openKnowledgeItem,
    fetchBlob,
    searchFiles,
  };
}

/** The chat's stacked-page and editor-tab routes. */
function useRouteOpeners({
  chatId,
  title,
  navigate,
  sourceLocation,
}: {
  chatId: string | null;
  title: string;
  navigate: NavigateFunction;
  sourceLocation: string;
}) {
  const openCompaction = useCallback(
    (part: CompactionConversationPart): void => {
      if (!chatId) return;
      const page = chatRoutes().compaction(chatId, part.id);
      navigate(stackedTarget(page, sourceLocation));
    },
    [chatId, navigate, sourceLocation],
  );

  const openPlan = useCallback(
    (part: QuestionConversationPart): void => {
      if (!chatId) return;
      // The editor owns plan documents when that host seam is available; a
      // browser preview falls back to the in-webview detail route.
      if (openPlanDocument(planDocumentOf(part, title))) return;
      const page = chatRoutes().plan(chatId, part.id);
      navigate(stackedTarget(page, sourceLocation));
    },
    [chatId, navigate, sourceLocation, title],
  );

  const openSubagent = useCallback(
    (childSessionId: string): void => {
      navigate(stackedTarget(chatRoutes().subagent(childSessionId), sourceLocation));
    },
    [navigate, sourceLocation],
  );

  const openResults = useMemo(() => {
    if (!chatId) return undefined;
    const page = chatRoutes().results(chatId);
    return () => navigate(stackedTarget(page, sourceLocation));
  }, [chatId, navigate, sourceLocation]);

  const openHistoryChat = useCallback(
    (picked: string): void => {
      void navigate(`/chat/${picked}`);
    },
    [navigate],
  );
  return { openCompaction, openPlan, openSubagent, openResults, openHistoryChat };
}
