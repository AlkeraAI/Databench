// The canonical chat frontend: the @alkera/ui organs assembled over the
// headless controller hooks.
// This file owns only view state the package leaves to the host: the
// composer's picked model/effort, the active question's live mirror, and the
// editor theme. Everything else translates through the entries builder.

import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError } from "@/api/errors";
import { browserOnline } from "@/lib/online";

import { MAX_CHAT_MESSAGE_CHARS } from "@alkera/sdk";
import type {
  AskCall,
  PermissionConversationPart,
  QuestionConversationPart,
} from "@alkera/chat-model";
import { findActiveQuestion, findGatedCall, findPendingPermissions } from "@alkera/chat-model";
import {
  ChatFilesProvider,
  ChatPanel,
  Composer,
  currentBrand,
  SuggestedAsks,
  Notice,
  PermissionCard,
  PlanApprovalCard,
  QuestionCard,
  type ChatFileRef,
  type ChromeLinkId,
  type ChromeMenuAction,
  type ComposerAttachment,
  type ComposerUploader,
  type PermissionModeSwitch,
  type QuestionLiveView,
  type PlanResolution,
  type QueuedComposerMessage,
} from "@alkera/ui";
import "@alkera/ui/chat/styles";
import "@alkera/ui/chat/fonts";

import { IconTemplate } from "@tabler/icons-react";

import { stripPermissionParts } from "./stripPermissionParts";
import { useLocation, useNavigate } from "react-router-dom";

import { useChatController, type ChatController } from "./controller";
import { useSharedDraft } from "./useSharedDraft";
import { useLiveRoleRefresh } from "./useLiveRoleRefresh";
import { carriedDraft, useStartChatWith } from "./startChatWith";
import { useStackedCrumbs } from "./crumbTrail";
import { ChatChrome } from "./ChatChrome";
import { useChatCopyAction } from "./useChatCopyAction";
import { useChatShare } from "./useChatShare";
import { SAVE_AS_TEMPLATE, useChatSaveAsTemplate } from "./useSaveAsTemplateAction";
import { useChatMenuRows } from "../../../app/extensions/portal";
import { SaveAsTemplateDialog } from "../files/SaveAsTemplateDialog";
import { ReconnectingNote } from "./ReconnectingNote";
import { TRANSCRIPT_UNREADABLE } from "./chatStore";
import { useDeleteChatAction } from "./useDeleteChatAction";
import { useHostDark } from "./useHostDark";
import { routeSlashLine, useSlashCommands } from "./slashCommands";
import { heldTurnIds, transcriptEntries } from "./entries";
import { useChatFailureNote } from "./useChatFailureNote";
import {
  MODE_CHOICES,
  MODE_OPTIONS,
  PLAN_REJECT_ANSWER,
  effortOptions,
  permissionCardProps,
  planChoicesOf,
  slashOptions,
} from "./options";
import { chatRoutes, performChromeAction } from "./chatRoutes";
import { useChromeLinks } from "./useChromeLinks";
import "./chat-surface.css";
import { chatCaps, chatData, chatHost, errorText } from "./data";
import { convergenceCheckEnabled, installConvergenceDetector } from "./data/convergenceDetector";
import { useChatFilesWiring } from "./chatFilesWiring";
import { materializedResults } from "./blobModel";
import { useChatFolderChanges } from "./useChatFolderChanges";
import type { ChatFileLocation } from "./data/chatFiles";
import { useNotebookChartSpec, useNotebookImage } from "./notebookImages";
import { revealNotebookCell } from "./workspace/notebook/cellAnchor";
import {
  APPROVAL_WITHHELD,
  switchableModes,
  type ApprovalVerdict,
} from "./data/ChatDataSource";

/** The opening screen where the shell offers none of its own. The editor's
 *  reader has a repository open in front of them; that is what this asks about.
 *  A shell whose reader has a warehouse rather than a working tree says so
 *  instead (`ChatHost.emptyState`) — the copy belongs to the surface, not to
 *  this composition. */


/** Whether a browser tab offers the door on a subagent's card into that child's
 *  own chat.
 *
 *  Off: the portal routes a child session at `/chat/<id>` like any other chat,
 *  which REPLACES the parent the reader is in and leaves them no way back to
 *  the turn that spawned it. The card keeps reporting the child's progress
 *  meanwhile. Flip this once the portal stacks a subagent chat over its parent
 *  the way the editor opens it beside one. */
const WEB_SUBAGENT_CHAT = false;

/** What the shell can say about the chat beyond its transcript. Both are the
 *  browser's concern today: the extension's daemon is the machine, so it has
 *  nothing to report about one. */
/** What stands where the composer did for a reader who may only follow the
 *  chat. One sentence, and the one thing they can go and do about it. */
export const READ_ONLY_DOCK_NOTE = "Ask the owner for edit access to send messages.";

/** What the working line says for a message no machine has taken up yet —
 *  the machine is not up, or it is up and has not got to this chat — until
 *  its turn starts. "Machine" is the one word the banner above it uses for the
 *  same thing, in every state it can be in. */
export const WAITING_FOR_MACHINE = "Waiting for a machine…";

/** Whether the chat's document says a turn is running — the box's own word,
 *  which it writes the moment it hands a message to the agent. */
function useTurnStarted(chatId: string | undefined): boolean {
  const [started, setStarted] = useState(false);
  useEffect(() => {
    if (!chatId) {
      setStarted(false);
      return;
    }
    const source = chatData();
    const read = (): void => setStarted(source.turnState?.(chatId) === "working");
    read();
    return source.subscribeChat(chatId, read);
  }, [chatId]);
  return started;
}

export interface ChatSurfaceProps {
  chatId?: string;
  /** Rendered under the chrome — the machine-state banner in the browser. */
  notice?: ReactNode;
  /** Who has the chat open, drawn at the top right under the chrome. The
   *  browser hands in its presence stack; the editor, whose chat has one
   *  reader, hands in nothing and the row does not render. */
  presence?: ReactNode;
  /** Where the chat's workspace runs, drawn in the header's action row. */
  machine?: ReactNode;
  /** Nothing can be sent right now (the machine is not ready). The composer
   *  stays in place, disabled, carrying `unavailableReason`. */
  unavailable?: boolean;
  unavailableReason?: string;
  /** The workspace behind this chat is not up yet (starting, restarting,
   *  reconnecting): a message sent now waits for it, and says so, until its
   *  turn starts. */
  machineWaiting?: boolean;
  /** What the working line says while a message waits for its turn to start.
   *  The shell names it when it knows more than "a machine" (a sleeping chat
   *  is waking); otherwise {@link WAITING_FOR_MACHINE}. */
  waitingLabel?: string;
  /** The workspace a chat started from this empty composer is made in. */
  newChatWorkspaceId?: string;
  /** The workspace this open chat is in. A new chat started from it on
   *  another model is started there too. */
  workspaceId?: string;
  /** Whether this reader may send in this chat at all, as the chat read says.
   *
   *  `false` takes the composer AND Stop away: a reader who may only follow the
   *  chat has no turn to stop and nothing to write, and a field they cannot
   *  type in is an invitation they keep trying to accept. `undefined` is the
   *  server not having said — a shell that does not read the chat row, or one
   *  answering from before the field existed — and leaves the dock exactly as
   *  it was, so an unknown never silently locks a reader out of their own chat.
   */
  canSend?: boolean;
  /** Whether the server says this reader may answer an ask with "Always"
   *  (the chat's own owner). `false` leaves the option off the card;
   *  `undefined` is a shell with no server read (a person's own computer),
   *  where the reader is the chat's owner. */
  canAnswerAlways?: boolean;
  /** Why this reader may not change the permission mode, when they may not.
   *  The chip then states the mode, cannot be operated, and carries this as
   *  its reason. */
  modeLockedReason?: string;
  /** Rename the open chat from its own title in the header. Unwired — the
   *  editor, and a reader who may only view this chat — the title is plain
   *  text. Rejecting with an Error puts its message under the title as the
   *  refusal. */
  onRenameTitle?: (title: string) => Promise<void>;
  /** Where a file named in the transcript opens, for a shell with somewhere of
   *  its own to put it. Unwired, a located file opens in the workspace pane. */
  onOpenChatFile?: (item: ChatFileLocation) => void;
  /** Where a file named in the transcript is shown in its folder. Unwired, it is
   *  revealed in the workspace pane's Files tab. */
  onRevealChatFile?: (ref: ChatFileRef) => void;
}

export function ChatSurface({
  chatId,
  notice,
  presence,
  machine,
  unavailable,
  unavailableReason,
  machineWaiting,
  waitingLabel,
  newChatWorkspaceId,
  workspaceId,
  canSend,
  canAnswerAlways,
  modeLockedReason,
  onRenameTitle,
  onOpenChatFile,
  onRevealChatFile,
}: ChatSurfaceProps = {}) {
  // `unavailable` is the shell's statement that the machine behind this chat is
  // not serving, which is also the one fact the stall watchdog cannot work out
  // for itself — a turn on a box that has gone is lost, not slow.
  const chat = useChatController({
    chatId,
    machineUnavailable: Boolean(unavailable),
    newChatWorkspaceId,
  });
  const dark = useHostDark();
  const navigate = useNavigate();
  // The words a "Start a new chat with <model>" carried over from another chat.
  const carried = carriedDraft(useLocation().state);
  const deleteOpenChat = useDeleteChatAction(chat.identity.chatId);
  // A role the owner changes re-reads what the composer is decided from.
  useLiveRoleRefresh(chat.identity.chatId ?? null);
  // A chat is a node in the drive, so sharing it is sharing that node at the
  // same four rungs every other file has. Only a shell that holds the dialog
  // asks for one — `null` everywhere else, and the Files reads behind it then
  // never run in a webview that has no portal API.
  const routedShare = chatRoutes().share;
  const share = useChatShare(
    routedShare?.kind === "share-dialog" ? (chat.identity.chatId ?? null) : null,
  );
  // Copying rides the same shell test as sharing: only a shell with the portal
  // API behind it (the one that holds the share dialog) can duplicate a chat.
  const copy = useChatCopyAction(
    routedShare?.kind === "share-dialog" ? (chat.identity.chatId ?? null) : null,
  );
  // And so does saving the chat as a template: the copy it makes lands in a
  // drive, which is the one thing the editor's webview has no API for.
  const template = useChatSaveAsTemplate(
    routedShare?.kind === "share-dialog" ? (chat.identity.chatId ?? null) : null,
  );
  // And an installed extension's rows (the product's hand-off to an editor), offered only
  // where the portal holds the chat.
  const extensionRows = useChatMenuRows(
    routedShare?.kind === "share-dialog" ? (chat.identity.chatId ?? null) : null,
  );

  const slash = useSlashCommands({
    chatId: chat.identity.chatId ?? "",
    currentMode: chat.composer.composerMode,
    currentTitle: chat.identity.currentTitle,
    daemonCommands: chat.composer.daemonCommands,
    exit: chat.nav.exitChat,
    onTitleChanged: chat.nav.refreshChatTitles,
  });

  // The controller owns the composer's choices (per-chat, store-backed), so
  // leaving the chat never wipes a pick.
  const model = chat.composer.defaultModel ?? "";
  const effort = chat.composer.effort ?? "";

  // The dock's active question feeds its transcript mirror live.
  const [questionLive, setQuestionLive] = useState<QuestionLiveView | null>(null);
  // A suggested ask is handed to the composer rather than sent from here, so it
  // goes through the one commit a typed message does: under the composer's
  // gate, with the reader's picks, and carrying whatever files are staged.
  const [offer, setOffer] = useState<{ text: string; at: number } | undefined>(undefined);
  const pendingPermissions = findPendingPermissions(chat.transcript.renderedTurns);
  const permissionCall = pendingPermissions[0]
    ? findGatedCall(chat.transcript.renderedTurns, pendingPermissions[0])
    : undefined;
  const displayTurns = stripPermissionParts(chat.transcript.renderedTurns);
  const heldTurns = useMemo(() => heldTurnIds(chat.transcript.renderedTurns), [chat.transcript.renderedTurns]);
  const activeQuestion =
    pendingPermissions.length > 0 ? undefined : findActiveQuestion(displayTurns);
  useEffect(() => {
    setQuestionLive(null);
  }, [activeQuestion?.requestId]);

  // Dev-only: diff the live fold against a fresh fold of the durable log while
  // the chat is open. `import.meta.env.DEV` folds to false on a production
  // build, so the whole branch is tree-shaken there.
  const openChatId = chat.identity.chatId;
  useEffect(() => {
    if (!import.meta.env.DEV || !openChatId || !convergenceCheckEnabled()) return undefined;
    return installConvergenceDetector(openChatId, chatData());
  }, [openChatId]);

  // A subagent's name lives in the spawn card that started it, not the chat list.
  const crumbs = useStackedCrumbs();

  const links = useChromeLinks(chat.transcript.renderedTurns);

  const openUrl = (url: string) => void chatHost().auth.openBrowser(url);
  const activeQuestionId = activeQuestion?.requestId;
  // Entry building parses every part; hold it on its real inputs so a
  // keystroke in the dock's question card does not rebuild the transcript.
  const workspaceRoot = chatHost().workspacePath() ?? undefined;
  const openFile = chatHost().openFile;
  const subagentChatOpens = WEB_SUBAGENT_CHAT || chatHost().kind === "vscode";
  // What a Retry resends: the last thing the reader actually said. The failed
  // turn already carries their bubble, so the retry re-asks the same words
  // rather than adding a second copy of them to the transcript.
  const lastUserText = useMemo(() => {
    for (let i = displayTurns.length - 1; i >= 0; i -= 1) {
      const turn = displayTurns[i];
      if (turn.author !== "user") continue;
      const said = turn.parts
        .map((part) => (part.kind === "text" ? part.text : ""))
        .join("")
        .trim();
      if (said) return said;
    }
    return null;
  }, [displayTurns]);
  const retryLastMessage = useMemo(
    () =>
      lastUserText
        ? () =>
            chat.transcript.submitMessage(lastUserText, {
              model,
              effort: effort || undefined,
              mode: chat.composer.composerMode,
            })
        : undefined,
    [lastUserText, chat.transcript, model, effort, chat.composer.composerMode],
  );
  // Sending a dropped message again is an ordinary send of those words — the
  // same path the composer uses, under the picks in force now, so the message
  // joins the conversation at the end rather than reviving a dead turn.
  const resendMessage = useCallback(
    (text: string) =>
      chat.transcript.submitMessage(text, {
        model,
        effort: effort || undefined,
        mode: chat.composer.composerMode,
      }),
    [chat.transcript, model, effort, chat.composer.composerMode],
  );
  const failureNote = useChatFailureNote({
    enabled: chatHost().kind !== "vscode",
    // The same gate the composer is under: a card must not offer to send what
    // the shell would refuse to take, and a Retry beside a disabled composer is
    // a button that answers a click with nothing.
    onRetry: unavailable ? undefined : retryLastMessage,
  });
  // The chat's files: how a pasted image goes up and comes back on screen. A
  // chat that does not exist yet is opened for its files on send, under the
  // same picks the first message would carry.
  const startChat = chat.transcript.startChat;
  const openChatForFiles = startChat
    ? (title: string) =>
        startChat(title, { model, effort: effort || undefined, mode: chat.composer.composerMode })
    : undefined;
  const chatFiles = useChatFilesWiring(chat.identity.chatId, openChatForFiles, {
    openItem: onOpenChatFile,
    revealItem: onRevealChatFile,
  });
  const notebookImage = useNotebookImage(chat.identity.chatId ?? null);
  const notebookChartSpec = useNotebookChartSpec(chat.identity.chatId ?? null);
  // "Go to cell" opens the notebook through the same door a chat file link
  // does, with the cell named in the page's anchor for the notebook to reveal.
  const openFileByPath = chatFiles.resolver?.openPath;
  const onOpenNotebookCell = useMemo(
    () =>
      openFileByPath
        ? (path: string, cellId: string) => {
            revealNotebookCell(cellId);
            openFileByPath(path);
          }
        : undefined,
    [openFileByPath],
  );
  const entries = useMemo(
    () =>
      transcriptEntries(displayTurns, {
        heldTurnIds: heldTurns,
        onOpenUrl: (url) => void chatHost().auth.openBrowser(url),
        // A path becomes a button only where the shell can open one. A page has
        // no editor, so the same card renders the path as the label it is
        // rather than a control that answers a click with nothing.
        onLinkClick: openFile ? (path: string) => void openFile(path) : undefined,
        workspaceRoot,
        onResourceOpen: chat.nav.openResource,
        // A subagent's card opens the child's own chat only where the shell can
        // come back from it. The editor stacks it as a tab beside the parent; a
        // browser tab would replace the chat the reader is reading, with the
        // parent's own transcript nowhere in the crumb trail, so the card there
        // states the child's progress and offers no door.
        onSubagentOpen: subagentChatOpens ? chat.nav.openSubagent : undefined,
        onPlanOpen: chat.nav.openPlan,
        onCompactionOpen: chat.nav.openCompaction,
        onLineageOpen: chat.nav.openLineageNode,
        onKnowledgeOpen: chat.nav.openKnowledgeItem,
        notebookImage,
        notebookChartSpec,
        onOpenNotebookCell,
        activeQuestionId,
        questionLive,
        failureNote,
        // The same gate the composer is under: a shell that would refuse the
        // send offers no Resend, so the message states that it was not sent
        // and stops there.
        onResend: unavailable ? undefined : resendMessage,
      }),
    [
      displayTurns,
      heldTurns,
      failureNote,
      resendMessage,
      unavailable,
      activeQuestionId,
      questionLive,
      chat.nav.openResource,
      chat.nav.openSubagent,
      subagentChatOpens,
      chat.nav.openPlan,
      chat.nav.openCompaction,
      chat.nav.openLineageNode,
      chat.nav.openKnowledgeItem,
      notebookImage,
      notebookChartSpec,
      onOpenNotebookCell,
      workspaceRoot,
      openFile,
    ],
  );

  // A path is looked up once per transcript, which is wrong while a machine is
  // writing the folder: a reference the agent wrote just before the file landed
  // would go on saying the file is not in the chat. The folder's own changes are
  // what drop that answer — and only the shell with the Files API behind it (the
  // same one that holds the share dialog) has a folder to watch.
  const chatFilesChanged = useChatFolderChanges(
    routedShare?.kind === "share-dialog" ? (chat.identity.chatId ?? null) : null,
  );
  // Where each held result was written out to, so a `blob:` reference the
  // agent wrote opens that file on a shell that cannot open the result itself.
  // Held on its content, not on every streamed turn.
  const resultFilesKey = JSON.stringify([...materializedResults(displayTurns)]);
  const resultFiles = useMemo(
    () => new Map<string, string>(JSON.parse(resultFilesKey) as [string, string][]),
    [resultFilesKey],
  );

  // The one path a composer line takes. `hold` is the mid-turn press: the
  // words are recorded against the chat and go out on the turn's end, under
  // the same picks they were typed with. A slash line is not a turn and is not
  // held — it runs where it was typed, whatever the agent is doing.
  const submit = (text: string, attachments?: string[], hold = false) => {
    // A slash line routes to the command registry (or straight to the daemon
    // for a command whose editor presentation is a panel); prose is a turn.
    const currentChatId = chat.identity.chatId;
    if (
      text.startsWith("/") &&
      currentChatId &&
      routeSlashLine(text, slash.commands, {
        run: slash.onCommandRun,
        daemon: (line) => void chatData().runCommand(currentChatId, line),
      })
    ) {
      return;
    }
    const meta = {
      model,
      effort: effort || undefined,
      mode: chat.composer.composerMode,
      attachments: attachments?.length ? attachments : undefined,
    };
    if (hold && chat.transcript.queueMessage) chat.transcript.queueMessage(text, meta);
    else chat.transcript.submitMessage(text, meta);
  };
  const send = (text: string, attachments?: string[]) => submit(text, attachments);
  const queueSend = chat.transcript.queueMessage
    ? (text: string, attachments?: string[]) => submit(text, attachments, true)
    : undefined;

  const dock = chat.nav.isSubagentView ? undefined : (
    <>
      {/* Above the composer, not over the transcript: a dropped connection is
          a condition of the thing the reader is about to press, and it clears
          itself. */}
      <ReconnectingNote />
      <ChatDock
      pendingPermissions={pendingPermissions}
      permissionCall={permissionCall}
      activeQuestion={activeQuestion}
      composer={chat.composer}
      interrupts={chat.interrupts}
      chatId={chat.identity.chatId}
      workspaceId={workspaceId}
      sending={chat.transcript.sending}
      model={model}
      effort={effort}
      slashCommands={slash.commands}
      onSend={send}
      onQueue={queueSend}
      queued={chat.transcript.queued}
      queuedBehindTurn={chat.transcript.queuedBehindTurn}
      onEditQueued={chat.transcript.editQueued}
      onRemoveQueued={chat.transcript.removeQueued}
      onSendQueued={chat.transcript.sendQueued}
      uploader={chatFiles.uploader}
      onStop={chat.transcript.cancelTurn}
      stopping={chat.transcript.stopping}
      onQuestionLive={setQuestionLive}
      onOpenUrl={openUrl}
      unavailable={unavailable}
      unavailableReason={unavailableReason}
      canSend={canSend}
      canAnswerAlways={canAnswerAlways}
      modeLockedReason={modeLockedReason}
      returnedDraft={chat.transcript.returnedDraft ?? (chatId ? null : carried)}
      offer={offer}
      />
    </>
  );

  const newChat = chatRoutes().newChat;
  // The key is only offered once the chat EXISTS — there is nothing to share
  // about a composer — and only in a shell that has somewhere to lead. Where
  // that shell holds the sharing dialog itself (the portal), the key opens it
  // over the chat's own node instead of navigating away from the chat.
  const onShareChat =
    routedShare && chat.identity.chatId
      ? routedShare.kind === "share-dialog"
        ? share.onShare
        : (): void => void performChromeAction(routedShare, navigate)
      : undefined;
  // The header's overflow rows. Saving the chat as a template earns no key on
  // the bar — it is asked for once a chat is worth repeating, not every turn —
  // so the menu is where it lives, and a shell that cannot save one offers no
  // row rather than a row that refuses.
  const moreActions: readonly ChromeMenuAction[] = [
    ...extensionRows.flatMap((row) => (row.action ? [row.action] : [])),
    ...(template.onSaveAsTemplate
      ? [
          {
            id: "save-template",
            label: SAVE_AS_TEMPLATE,
            icon: <IconTemplate size={13} stroke={1.8} aria-hidden />,
            onSelect: template.onSaveAsTemplate,
          },
        ]
      : []),
  ];
  const chrome = (
    <ChatChrome
      title={chat.identity.title}
      // Only a chat that exists has a name to change: the empty composer's
      // header carries the shell's own heading, not a renameable chat.
      onRenameTitle={chat.identity.chatId ? onRenameTitle : undefined}
      trail={crumbs}
      links={links}
      // A new chat starts from home, whose composer is the new-chat door; the
      // key leads there rather than to a blank chat route. Where the shell
      // carries no such key — a portal page, whose app shell already has one —
      // the chrome renders none.
      onNewChat={newChat ? () => void performChromeAction(newChat, navigate) : undefined}
      onShareChat={onShareChat}
      machine={machine}
      onCopyChat={copy.onCopy}
      copyChatLabel={copy.label}
      moreActions={moreActions}
      onDeleteChat={deleteOpenChat.onDelete}
      onOpenLink={(id) => openChromeLink(id, chat.nav.openResults, navigate)}
    />
  );
  const header = (
    <>
      {chrome}
      {/* The dialog the header's Share key opens. It lives out here rather than
          in the chrome: the chrome is the component both shells render, and a
          modal over the portal's drive is one shell's alone. */}
      {share.dialog}
      {/* Out here for the same reason: the naming step before a copy is made
          belongs to the shell that has a drive to copy into. */}
      {copy.dialog}
      {/* And the same for the template: one drive, one dialog, one question. */}
      <SaveAsTemplateDialog {...template.dialogProps} />
      {/* And the offer of the extension, when a link to VS Code went unanswered. */}
      {extensionRows.map((row) => (
        <Fragment key={row.key}>{row.dialog}</Fragment>
      ))}
      {/* The faces of everyone in the chat, under the chrome carrying the key
          that lets more people in. */}
      {presence ? <div className="chat-topbar">{presence}</div> : null}
      {notice}
      <ChatProblems composer={chat.composer} transcript={chat.transcript} />
    </>
  );
  const working = chat.transcript.sending && pendingPermissions.length === 0 && !activeQuestion;
  const machineSaysWorking = useTurnStarted(chatId);
  // "Working…" is the machine's to say: it has started a turn (its document
  // says so), or the tape shows something it wrote after the reader's
  // message. Until then the message is waiting for the workspace — whether
  // the workspace is down or up and busy elsewhere — and a reader told the
  // agent is working for six minutes over a message nothing had picked up was
  // told something the record never said.
  const waitingForWorkspace =
    !machineSaysWorking &&
    (Boolean(machineWaiting) ||
      (chatData().startsTurnsRemotely === true && chat.transcript.awaitingTurnStart));
  // A suggestion is a send, so it is pressable exactly when the composer
  // would take one: not while the machine is unavailable, not for a reader who
  // may not send, and not where there is no composer to send it through.
  // The empty chat's suggestions only for a chat known to have no messages: a
  // transcript that failed to load says so, with a way to ask again.
  const empty = chat.transcript.loadFailed ? (
    <div className="chat-load-failed">
      <Notice
        level="warning"
        title={TRANSCRIPT_UNREADABLE}
        body=""
        action={{ label: "Retry", onClick: chat.transcript.retryLoad }}
      />
    </div>
  ) : (
    <SuggestedEmpty
      prompts={chat.transcript.suggestedPrompts}
      disabled={Boolean(unavailable) || canSend === false || dock === undefined}
      onPick={(prompt) => setOffer((last) => ({ text: prompt.prefill, at: (last?.at ?? 0) + 1 }))}
    />
  );

  return (
    <div className="chat-root chat-shell-fill" data-theme={dark ? "dark" : undefined}>
      {/* The resolver wraps the transcript AND the dock: the reader's own
          bubble shows the image they pasted through the same door the
          agent's charts come through. */}
      {/* The delete question, mounted once outside the header: a header that
          re-renders must not take the open dialog with it. */}
      {deleteOpenChat.dialog}
      <ChatFilesProvider
        resolver={chatFiles.resolver}
        invalidate={chatFilesChanged}
        chatId={chat.identity.chatId ?? null}
        resultFiles={resultFiles}
      >
        <ChatPanel
          entries={entries}
          working={working}
          workingLabel={waitingForWorkspace ? (waitingLabel ?? WAITING_FOR_MACHINE) : undefined}
          header={header}
          dock={dock}
          empty={empty}
          loading={chat.transcript.loadingChat}
          history={chat.transcript.history}
        />
      </ChatFilesProvider>
    </div>
  );
}

// The chrome's quick links. Where knowledge and lineage live is the shell's to
// say (`chatRoutes()`): the editor opens its own tabs through a command, the
// portal navigates to its page. The chat's own results are a stacked surface in
// both, so they go through the controller. A link the shell has no surface for
// is never rendered, so `null` here would be a key that should not exist.
function openChromeLink(
  id: ChromeLinkId,
  openResults: (() => void) | undefined,
  navigate: (path: string) => void,
): void {
  if (id === "artifacts") {
    openResults?.();
    return;
  }
  const target = id === "knowledge" ? chatRoutes().knowledge : chatRoutes().lineage;
  if (target) performChromeAction(target, navigate);
}

interface ChatDockProps {
  pendingPermissions: PermissionConversationPart[];
  /** The tool call the front ask gates, when the transcript holds it. */
  permissionCall?: AskCall;
  activeQuestion: QuestionConversationPart | undefined;
  composer: ChatController["composer"];
  interrupts: ChatController["interrupts"];
  chatId: string | null;
  /** The open chat's workspace, where a new chat on another model starts. */
  workspaceId?: string;
  sending: boolean;
  model: string;
  effort: string;
  slashCommands: Parameters<typeof slashOptions>[0];
  onSend: (text: string, attachments?: string[]) => void;
  /** Hold a message typed mid-turn. Absent where there is no chat to hold it
   *  against — the composer then keeps the words in the field. */
  onQueue?: (text: string, attachments?: string[]) => void;
  /** What this chat is already holding, drawn under the field. */
  queued: QueuedComposerMessage[];
  /** A message this reader already sent is waiting behind the running turn. */
  queuedBehindTurn: boolean;
  onEditQueued?: (id: string, text: string) => void;
  onRemoveQueued?: (id: string) => void;
  /** Send a message a previous page session left behind. */
  onSendQueued?: (id: string) => void;
  onStop: () => void;
  /** A Stop already pressed and not yet answered by the source. */
  stopping: boolean;
  onQuestionLive: (view: QuestionLiveView | null) => void;
  onOpenUrl: (url: string) => void;
  unavailable?: boolean;
  unavailableReason?: string;
  canSend?: boolean;
  canAnswerAlways?: boolean;
  modeLockedReason?: string;
  /** Words the surface is handing back to the field — a first message whose
   *  chat could not be started. It outranks the chat's shared draft: the
   *  reader's own unsent sentence is never displaced by what the document was
   *  holding before they typed it. */
  returnedDraft?: { text: string; at: number } | null;
  onAttach?: (files: File[]) => void;
  attachments?: ComposerAttachment[];
  onRemoveAttachment?: (id: string) => void;
  /** Where a pasted image or file goes; absent where the chat has no folder yet. */
  uploader?: ComposerUploader;
  /** A suggested ask to send through the composer. */
  offer?: { text: string; at: number };
}

/** What the composer's slot shows: a pending permission outranks a question,
 *  a question outranks the composer itself. */
/** Under the model menu when the chat's box applies a switch only once the
 *  chat's agent restarts (a box on an older build). */
const SWITCH_AFTER_RESTART_NOTE = "Applies after the chat restarts.";
/** Under the model menu for a reader who is not the chat's owner: the owner
 *  pays for its turns, so the list is the owner's plan, not theirs. */
const OWNERS_PLAN_NOTE = "Models on the chat owner's plan.";

/** What the card says when the machine that raised the ask has gone. The keys
 *  stay on screen and stop working: an answer has nowhere to arrive. */
const MACHINE_GONE_NOTE = "The machine is not reachable, so this cannot be answered.";

/** What a question or plan card says when the ask it took an answer for is no
 *  longer open — it timed out on the machine, or the transcript already holds a
 *  decision for it. Only these two cards use it: a permission ask settles
 *  instead, and has an `expirePermission` seam to settle with. */
const ANSWER_CLOSED_NOTE = "This ask is no longer waiting for an answer.";

/** The one-sentence reason an answer did not reach the machine, or null when
 *  there is no ask left to answer and the card should simply settle. */
export function answerRefusal(err: unknown): string | null {
  if (!browserOnline()) {
    return "You are offline. The answer was not sent.";
  }
  if (err instanceof ApiError) {
    // The ask is gone — it timed out on the box, or someone else answered it.
    if (err.status === 404 || err.status === 410) return null;
    // The transcript already holds a decision for this ask: a card that
    // offered Retry here could only 409 again, so it settles instead and the
    // recorded resolution is what the reader sees.
    if (err.status === 409 && err.code === "ask_already_answered") return null;
    if (err.status === 403) return "You are not allowed to answer this request.";
    // Nothing is wrong with the answer: the limiter refused this one attempt
    // and the ask is still outstanding. Named rather than left to the generic
    // sentence, because what the reader does about it is wait a beat and press
    // again, which "The answer could not be sent." does not tell them.
    if (err.status === 429) return "Answering too quickly. Try again in a moment.";
  }
  return "The answer could not be sent.";
}

/** The permission ask, answered as a tracked write.
 *
 *  One answer at a time, with a refusal said on the card and the retry beside
 *  it, so a failed answer never vanishes into an unhandled rejection. */
function PermissionDock({
  part,
  call,
  queue,
  interrupts,
  onOpenUrl,
  approval,
  canAnswerAlways,
  unavailable,
  mode,
}: {
  part: PermissionConversationPart;
  call: AskCall | undefined;
  queue: { position: number; of: number } | undefined;
  interrupts: ChatDockProps["interrupts"];
  onOpenUrl: (url: string) => void;
  approval: ApprovalVerdict;
  canAnswerAlways: boolean;
  unavailable?: string;
  mode?: PermissionModeSwitch;
}) {
  // The dock is keyed on the request id where it is rendered, so the next ask
  // of a stack mounts with state of its own: nothing of the last one's
  // refusal or in-flight press carries, not even for the frame an effect
  // would take to clear it.
  const [sending, setSending] = useState<string | null>(null);
  const [failed, setFailed] = useState<{ optionId: string; reason: string } | null>(null);
  const { requestId } = part;
  const { resolvePermission, expirePermission } = interrupts;

  const answer = useCallback(
    (optionId: string): void => {
      setSending(optionId);
      setFailed(null);
      void resolvePermission(requestId, optionId)
        .catch((err: unknown) => {
          const reason = answerRefusal(err);
          // Nothing is holding the ask any more, so there is nothing to retry:
          // the card settles where it stands rather than offering a key that
          // would fail the same way.
          if (reason === null) expirePermission(requestId);
          else setFailed({ optionId, reason });
        })
        .finally(() => setSending(null));
    },
    [requestId, resolvePermission, expirePermission],
  );

  const busy = sending !== null;
  // The notebook an ask names is a link only where the host has an editor.
  const openFile = chatHost().openFile;
  const onOpenFile = openFile ? (path: string) => void openFile(path) : undefined;
  return (
    <PermissionCard
      {...permissionCardProps(part, queue, answer, onOpenUrl, approval, call, canAnswerAlways, onOpenFile)}
      busy={busy}
      unavailable={unavailable}
      failure={failed?.reason}
      onRetry={failed && !busy ? () => answer(failed.optionId) : undefined}
      mode={mode}
    />
  );
}

function ChatDock({
  pendingPermissions,
  permissionCall,
  activeQuestion,
  composer,
  interrupts,
  chatId,
  workspaceId,
  sending,
  model,
  effort,
  slashCommands,
  onSend,
  onQueue,
  queued,
  queuedBehindTurn,
  onEditQueued,
  onRemoveQueued,
  onSendQueued,
  onStop,
  stopping,
  onQuestionLive,
  onOpenUrl,
  unavailable,
  unavailableReason,
  canSend,
  canAnswerAlways,
  modeLockedReason,
  returnedDraft,
  onAttach,
  attachments,
  onRemoveAttachment,
  uploader,
  offer,
}: ChatDockProps) {
  // Before the early returns: a chat's shared draft is followed for as long as
  // the chat is open, not only while the composer happens to be the thing in
  // the slot. A permission card in front of it does not mean the other people
  // in the chat stopped typing.
  const sharedDraft = useSharedDraft(chatId);
  // The words in the field, so "Start a new chat with <model>" carries them.
  const typed = useRef("");
  const startChatWith = useStartChatWith(workspaceId);
  const models = composer.modelOptions.map((option) => ({
    value: option.value,
    label: option.label,
    unavailable: option.unavailable,
    escape: option.escapeModel
      ? {
          label: `Start a new chat with ${option.label}`,
          onSelect: () => startChatWith(option.escapeModel ?? option.value, typed.current),
        }
      : undefined,
  }));
  const allowedModes = switchableModes(chatCaps());
  const modeOptions = MODE_OPTIONS.filter((option) => allowedModes.has(option.value));
  // Before every other thing the dock can hold. A reader who may not send may
  // not answer a permission prompt or a question either — both are decisions
  // about a turn that is not theirs — and has no turn of their own to stop.
  if (canSend === false) {
    return <p className="chat-dock__readonly">{READ_ONLY_DOCK_NOTE}</p>;
  }
  // The mode moves from the card as it does from the composer the card hides:
  // the machine decides the ask again under the new mode, so a switch settles
  // it when the new mode answers it on its own and leaves it here when not.
  const modeSwitch: PermissionModeSwitch | undefined =
    composer.composerMode && modeOptions.length > 0 && !modeLockedReason
      ? {
          label: "Permission mode",
          value: composer.composerMode,
          options: modeOptions,
          onChange: composer.changeMode,
        }
      : undefined;
  const permission = pendingPermissions[0];
  const queue =
    pendingPermissions.length > 1 ? { position: 1, of: pendingPermissions.length } : undefined;
  // An ask takes the dock, and the composer stays mounted behind it. Unmounting
  // it dropped whatever a person had typed the moment a permission card came
  // up, and the draft was gone when the card cleared. Hidden, the composer is
  // out of the accessibility tree and cannot take focus while the ask has it.
  const ask = permission ? (
    <PermissionDock
      key={permission.requestId}
      part={permission}
      call={permissionCall}
      queue={queue}
      interrupts={interrupts}
      onOpenUrl={onOpenUrl}
      approval={chatId ? chatData().mayAllow(chatId, permission) : APPROVAL_WITHHELD}
      canAnswerAlways={canAnswerAlways !== false}
      unavailable={unavailable ? (unavailableReason ?? MACHINE_GONE_NOTE) : undefined}
      mode={modeSwitch}
    />
  ) : activeQuestion ? (
    <QuestionDock
      key={activeQuestion.requestId}
      question={activeQuestion}
      onAnswer={interrupts.answerQuestion}
      onLive={onQuestionLive}
    />
  ) : null;
  return (
    <>
      {ask}
      <div hidden={ask !== null} className="chat-dock__held-composer">
    <Composer
      busy={sending}
      // The server's own ceiling, read out of the schema it publishes rather
      // than copied into the client.
      maxMessageChars={MAX_CHAT_MESSAGE_CHARS}
      // The shell's own invitation where it has one; the editor keeps the
      // composer's default.
      placeholder={chatHost().emptyState?.placeholder}
      unavailable={unavailable}
      unavailableReason={unavailableReason}
      // Only the modes this source will actually put a session into. The
      // browser narrows to the three whose worst case is still an ask, so the
      // picker cannot offer a stance the server would refuse; a source that
      // fixes the mode offers none, and the chip states it instead.
      // An empty mode is a stance the composer cannot name yet (a new chat
      // whose seed is still in flight): the chip leaves the rail rather than
      // showing the first option as if it were the answer.
      modes={composer.composerMode ? modeOptions : []}
      mode={composer.composerMode}
      onModeChange={
        composer.composerMode && modeOptions.length > 0 && !modeLockedReason
          ? composer.changeMode
          : undefined
      }
      modeLockedReason={modeLockedReason}
      models={models}
      model={model}
      modelHint={
        [
          composer.modelsFromOwnersPlan ? OWNERS_PLAN_NOTE : null,
          composer.switchApplies === "after_reopen" ? SWITCH_AFTER_RESTART_NOTE : null,
        ]
          .filter(Boolean)
          .join(" ") || undefined
      }
      // No handler where the source will not move an open chat onto another
      // model: the chip still states what the session runs under and opens
      // nothing, rather than taking a pick that would land nowhere. The effort
      // rides the same pin, so it locks with it.
      onModelChange={composer.modelLocked ? undefined : composer.changeModel}
      efforts={effortOptions(composer.efforts)}
      effort={effort}
      onEffortChange={composer.modelLocked ? undefined : composer.changeEffort}
      slashCommands={chatId ? slashOptions(slashCommands) : []}
      onSend={onSend}
      // Enter during a turn is held rather than dropped. Where the shell has
      // nowhere to hold it, the composer keeps the words in the field and
      // sends them itself on the turn's end.
      onQueue={onQueue}
      // A send taken over a running turn is recorded and then waits for it.
      // The field has already cleared, so this line is the only thing that
      // tells the reader their message arrived.
      queuedBehindTurn={queuedBehindTurn}
      queued={queued}
      onEditQueued={onEditQueued}
      onRemoveQueued={onRemoveQueued}
      onSendQueued={onSendQueued}
      onStop={onStop}
      // The press has gone and nothing has come back: the key says so rather
      // than flipping straight to Send under a reader who cannot tell whether
      // it was heard.
      stopping={stopping}
      onAttach={onAttach}
      attachments={attachments}
      onRemoveAttachment={onRemoveAttachment}
      uploader={uploader}
      offer={offer}
      // The chat's shared draft: the live binding, or, until there is one,
      // where this reader's typing goes and why it is theirs alone. Absent on
      // a shell whose chat has one reader.
      {...sharedDraft}
      onDraftChange={(text: string) => {
        typed.current = text;
        sharedDraft.onDraftChange?.(text);
      }}
      draft={returnedDraft ?? undefined}
    />
      </div>
    </>
  );
}

/** A question card and a plan card are answered as tracked writes, exactly as a
 *  permission ask is.
 *
 *  A refusal says why on the card and keeps the keys. */
function QuestionDock({
  question,
  onAnswer,
  onLive,
}: {
  question: QuestionConversationPart;
  onAnswer: (requestId: string, answers: string[][], note?: string | null) => Promise<void>;
  onLive: (view: QuestionLiveView | null) => void;
}) {
  // The dock is keyed on the request id where it is rendered, so the next ask
  // of a stack mounts with none of the last one's refusal on it.
  const [failure, setFailure] = useState<string | undefined>(undefined);
  // The plan card shows the answer it took from the moment it is pressed, so a
  // second press has nothing to land on; a refusal puts the choice back.
  const [sentPlan, setSentPlan] = useState<PlanResolution | null>(null);
  const { requestId } = question;

  const answer = useCallback(
    (answers: string[][]): void => {
      setFailure(undefined);
      // Clearing first and setting on the rejection is what lets the SAME
      // sentence land twice: the card's unlock watches the prop change.
      void onAnswer(requestId, answers).catch((err: unknown) => {
        setFailure(answerRefusal(err) ?? ANSWER_CLOSED_NOTE);
      });
    },
    [requestId, onAnswer],
  );

  if (question.questionKind === "plan_approval") {
    const prompt = question.questions[0];
    const { options, wireLabelByMode } = prompt
      ? planChoicesOf(prompt)
      : { options: [], wireLabelByMode: {} };
    // The wire fills the ask with the whole plan document; the card already
    // renders the plan, so a duplicated ask falls back to the standing one.
    const duplicated =
      !prompt?.question || prompt.question.trim() === (question.planMarkdown ?? "").trim();
    const ask = duplicated ? "Approve this plan?" : prompt.question;
    return (
      <PlanApprovalCard
        content={question.planMarkdown ?? ""}
        question={ask}
        options={options}
        planMode={MODE_CHOICES.plan}
        resolution={sentPlan}
        failure={failure}
        onResolve={(next) => {
          if (sentPlan) return;
          setSentPlan(next);
          setFailure(undefined);
          const label =
            next.kind === "approved" ? (wireLabelByMode[next.mode.mode] ?? next.mode.label) : PLAN_REJECT_ANSWER;
          void onAnswer(requestId, [[label]], next.note ?? null).catch((err: unknown) => {
            setSentPlan(null);
            setFailure(answerRefusal(err) ?? ANSWER_CLOSED_NOTE);
          });
        }}
      />
    );
  }
  return (
    <QuestionCard
      questions={question.questions}
      onSubmit={answer}
      onLive={onLive}
      failure={failure}
    />
  );
}

function ChatProblems({
  composer,
  transcript,
}: {
  composer: ChatController["composer"];
  transcript: ChatController["transcript"];
}) {
  const showModels = composer.modelsProblem && !composer.modelsAlertDismissed;
  const modelsBody = composer.modelsErrored
    ? `Couldn't load models. ${errorText(composer.modelsError)}`
    : "No models available from the model gateway.";
  return (
    <>
      {showModels ? (
        <Problem
          title="Models unavailable"
          body={modelsBody}
          onDismiss={composer.dismissModelsAlert}
        />
      ) : null}
      {composer.commandError ? (
        <Problem
          title={composer.commandError.title}
          body={composer.commandError.reason}
          onDismiss={composer.dismissCommandError}
        />
      ) : null}
      {transcript.cancelNotice ? (
        <Problem
          title="Stopped waiting"
          body={transcript.cancelNotice}
          onDismiss={transcript.dismissCancelNotice}
        />
      ) : null}
    </>
  );
}

function Problem({
  title,
  body,
  onDismiss,
}: {
  title: string;
  body: string;
  onDismiss: () => void;
}) {
  return (
    <div className="chat-shell-problem">
      <Notice level="danger" title={title} body={body} onDismiss={onDismiss} />
    </div>
  );
}

function SuggestedEmpty({
  prompts,
  disabled,
  onPick,
}: {
  prompts: ChatController["transcript"]["suggestedPrompts"];
  disabled: boolean;
  onPick: (prompt: ChatController["transcript"]["suggestedPrompts"][number]) => void;
}) {
  const pick = (index: number): void => {
    const prompt = prompts[index];
    if (prompt) onPick(prompt);
  };
  return (
    <SuggestedAsks
      title={chatHost().emptyState?.title ?? `What should ${currentBrand().productName} build?`}
      suggestions={prompts.map((prompt) => prompt.label)}
      disabled={disabled}
      onPick={(_suggestion, index) => pick(index)}
    />
  );
}
