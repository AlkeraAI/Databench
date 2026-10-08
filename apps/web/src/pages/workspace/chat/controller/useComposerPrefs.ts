// The composer's inputs and choices: the model/effort catalog, the slash
// command registry, the mode pill, and the transient errors those produce.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DRAFT_CHAT_KEY, useChatStore } from "../chatStore";
import { useRememberedDraftPicks } from "../draftPicks";
import { toPermissionMode } from "../adapters";
import { chatKeys } from "../chatKeys";
import { reasoningIsVisible, resolveModelChoice, type ChoiceModel } from "../modelChoice";
import type { ChatHandoff } from "../openSurfaces";
import { chatCaps, chatData, chatHost, type Chat, type ModelInfo, type SlashCommandInfo, errorText, refetchWhileErrored, refetchWhileErroredOrEmpty, refetchWhileNoChatDefault } from "../data";
import { servesModels, switchableModes, switchesModel } from "../data/ChatDataSource";
import { queryRetryDelay } from "@/api/retry";

/** One row of the composer's model picker. On an open chat whose source
 *  decides switches (`modelOptions`), a model the chat may not move to carries
 *  the reason, and the model a new chat would use instead. */
export interface ComposerModelChoice {
  value: string;
  label: string;
  unavailable?: string;
  escapeModel?: string;
}

export interface ChatComposer {
  /** The raw catalog — composition wiring for the transcript's send options,
   *  not part of the controller's public surface. */
  models: ModelInfo[];
  modelOptions: ComposerModelChoice[];
  /** When a switch on this open chat takes effect, where the source says it
   *  waits for the chat's agent to restart; otherwise undefined. */
  switchApplies?: "after_reopen";
  /** The open chat's models are its owner's plan, not this reader's. */
  modelsFromOwnersPlan?: boolean;
  modelsLoading: boolean;
  modelsErrored: boolean;
  modelsError: unknown;
  /** The catalog is errored OR (on a new chat) empty — surfaces the banner. */
  modelsProblem: boolean;
  modelsAlertDismissed: boolean;
  dismissModelsAlert: () => void;
  /** A composer action that did not take: what it was, in the title, and
   *  the reason the server gave, in the body. */
  commandError: ComposerProblem | null;
  dismissCommandError: () => void;
  daemonCommands: SlashCommandInfo[];
  reasoningVisible: boolean;
  composerMode: string;
  defaultModel: string | undefined;
  /** The window the chat's pinned model carries, when the source records one.
   *  The composer's context readout spends the conversation's tokens against
   *  it; absent, the readout states the count and draws no share. */
  contextWindow: number | undefined;
  modelLocked: boolean;
  efforts: string[];
  effort: string | undefined;
  changeModel: (model: string) => void;
  changeEffort: (effort: string) => void;
  /** The plain mode switch. The composition root wraps it with the bypass
   *  auto-allow sweep, which needs the transcript and the interrupts. */
  changeMode: (mode: string) => void;
}

/** What a refused composer action says: the action that did not take, and
 *  why, in the server's own words where it gave any. */
export interface ComposerProblem {
  title: string;
  reason: string;
}

/** The problem a failed action leaves, from the error it failed with. */
export function composerProblem(title: string, err: unknown): ComposerProblem {
  return { title, reason: errorText(err) };
}

export function useComposerPrefs({
  chatId,
  currentChat,
  handoff,
}: {
  chatId: string | null;
  currentChat: Chat | undefined;
  handoff: ChatHandoff | null;
}): ChatComposer {
  const queryClient = useQueryClient();
  const catalog = useComposerCatalog();
  const { models, modelOptions } = catalog;

  // The composer's choices (mode pill, model, effort) live in the chat store,
  // keyed per chat, so leaving a chat never wipes them: the daemon owns the
  // durable copy for an existing chat and pushes flips; this webview copy keeps
  // the pill steady across navigation and is the only home a draft chat has.
  const chatKey = chatId ?? DRAFT_CHAT_KEY;
  // A draft's picks survive a reload until the chat they were made for exists.
  useRememberedDraftPicks();
  const composerPrefs = useChatStore((state) => state.composerPrefs[chatKey]);
  const setComposerPref = useChatStore((state) => state.setComposerPref);

  const modelsErrored = catalog.modelsErrored;
  const noModels = !chatId && servesModels(chatCaps()) && catalog.modelsReady && modelOptions.length === 0;
  const modelsProblem = modelsErrored || noModels;
  const alerts = useComposerAlerts({ chatId, modelsProblem });
  const {
    commandError,
    setCommandError,
    dismissCommandError,
    modelsAlertDismissed,
    dismissModelsAlert,
  } = alerts;

  useModeSync({ chatId, setComposerPref, setCommandError });
  const pinnedModel = currentChat?.model;
  const choice = useModelChoice({
    chatId,
    pinnedModel,
    models,
    composerPrefs,
    handoff,
    chatDefaults: catalog.chatDefaults,
  });

  // A draft carries the pick into creation. On an existing chat the pick is
  // PERSISTED where the source can hold it: the chat row is what the box reads
  // to open the session, so a switch that only moved this component's state
  // would leave the reader looking at a chip saying one model over a transcript
  // another one wrote — and the next turn silently billed at the old model's
  // rate. A source with nowhere to put the switch greys the picker instead
  // (`modelLocked`), so there is no pick here to drop.
  const options = useModelOptions(chatId);
  // The verdicts follow the chat's history: a turn that just produced
  // reasoning can take a model off the list, so they are read again whenever
  // the chat moves on rather than served from before the turn.
  const historyMark = `${currentChat?.lastEventId ?? ""}|${currentChat?.updatedAt ?? ""}`;
  useEffect(() => {
    if (chatId) void queryClient.invalidateQueries({ queryKey: chatKeys.modelOptions(chatId) });
  }, [chatId, historyMark, queryClient]);
  const { mutate: switchModelTo } = useModelSwitch(chatId);
  const changeModel = useCallback(
    (model: string): void => {
      const prior = composerPrefs?.model;
      setComposerPref(chatKey, { model, effort: undefined });
      if (!chatId || !chatData().setModel) return; // a pre-create pick rides the CREATE
      // The model this reader switched FROM, so a chat somebody else moved in
      // between is refused rather than switched over their pick.
      const from = options.data?.currentModelId ?? currentChat?.model?.id ?? null;
      switchModelTo(
        { chatId, model, from },
        {
          onError: (err: unknown) => {
            // Refused (the chat's reasoning, a race, an outage): the chip goes
            // back to the model the chat is still on.
            setComposerPref(chatKey, { model: prior, effort: undefined });
            setCommandError(composerProblem("Couldn't switch the model", err));
          },
        },
      );
    },
    [chatId, chatKey, composerPrefs?.model, currentChat?.model?.id, options.data, switchModelTo, setComposerPref, setCommandError],
  );

  const changeEffort = useCallback(
    (effort: string): void => {
      setComposerPref(chatKey, { model: choice.defaultModel, effort });
      if (!chatId) return; // a pre-create choice rides the first send's options
      void chatData()
        .setEffort(chatId, effort)
        .catch((err: unknown) => setCommandError(composerProblem("Couldn't save the effort", err)));
    },
    [chatId, chatKey, choice.defaultModel, setComposerPref, setCommandError],
  );

  // Apply a mode switch to the LIVE session immediately (parity with effort),
  // so flipping mid-turn takes effect on the running turn instead of only
  // riding the next send. A pre-create choice (no chat yet) still rides the
  // first send's options.
  const changeMode = useCallback(
    (mode: string): void => {
      // The pick this one replaces, so a switch the server refuses puts the
      // chip back where it was rather than stating a stance nothing runs in.
      const prior = composerPrefs?.mode;
      setComposerPref(chatKey, { mode });
      if (!chatId) return;
      const persist = chatData().setPermissionMode(chatId, mode);
      void persist
        .then(() => patchChatListMode(queryClient, chatId, mode))
        .catch((err) => {
          setComposerPref(chatKey, { mode: prior });
          setCommandError(composerProblem("Couldn't switch the mode", err));
        });
    },
    [chatId, chatKey, composerPrefs?.mode, queryClient, setComposerPref, setCommandError],
  );

  // On an open chat whose source decides switches, the picker is the server's
  // verdicts: the models the chat may move to first, in the server's order,
  // then the rest grouped under the one line that says why; elsewhere it is
  // the catalogue.
  const pickerOptions = useMemo<ComposerModelChoice[]>(() => {
    const verdicts = options.data?.options;
    if (!chatId || !verdicts || verdicts.length === 0) return modelOptions;
    const reasonOf = (o: (typeof verdicts)[number]): string =>
      o.groupMessage ?? o.message ?? "Not available for this chat.";
    const reasons = [...new Set(verdicts.filter((o) => o.state === "unavailable").map(reasonOf))];
    const ordered = [
      ...verdicts.filter((o) => o.state !== "unavailable"),
      ...reasons.flatMap((reason) => verdicts.filter((o) => o.state === "unavailable" && reasonOf(o) === reason)),
    ];
    return ordered.map((o) => ({
      value: o.model.id,
      label: o.model.displayName,
      ...(o.state === "unavailable"
        ? { unavailable: reasonOf(o), escapeModel: o.escapeNewChatModel ?? undefined }
        : {}),
    }));
  }, [chatId, options.data, modelOptions]);

  return {
    models,
    modelOptions: pickerOptions,
    switchApplies: chatId && options.data?.applies === "after_reopen" ? "after_reopen" : undefined,
    modelsFromOwnersPlan: chatId && options.data?.billedToOwner ? true : undefined,
    modelsLoading: catalog.modelsLoading,
    modelsErrored,
    modelsError: catalog.modelsError,
    modelsProblem,
    modelsAlertDismissed,
    dismissModelsAlert,
    commandError,
    dismissCommandError,
    daemonCommands: catalog.daemonCommands,
    reasoningVisible: choice.reasoningVisible,
    // The stance this composer states AND acts on: a pick, else the open chat's
    // persisted mode. On an OPEN chat nothing rides a prompt, so the source's
    // fixed mode holds the chip while the per-chat read is in flight rather than
    // flashing "Default" over a session opened read-only.
    //
    // Before there IS a chat the same value rides the CREATE, so the floor may
    // not fill the gap: it is this surface guessing at the stance the server
    // resolves from the reader's saved default, and a create that names the
    // guess writes it onto the chat row in place of that default. The answer is
    // the new-chat seed; until it lands the mode is empty — no stance is named
    // on the create and the chip states none — and a source that has no seed
    // coming (it decides the stance when the session opens) keeps the product
    // default it always had.
    composerMode:
      composerPrefs?.mode ??
      currentChat?.permissionMode ??
      (chatId
        ? (chatCaps().fixedPermissionMode ?? "default")
        : (catalog.chatDefaults?.permissionMode ?? (chatCaps().fixedPermissionMode ? "" : "default"))),
    defaultModel: choice.defaultModel,
    contextWindow: pinnedModel?.contextWindow || undefined,
    // Locked wherever the source will not move an open chat: the two chips
    // become readouts of what the session actually runs under. The effort rides
    // the same pin (one row, one PUT), so the same fact governs both. Where the
    // source DOES move a chat (the harness respawns on the new pin), neither is
    // locked and the next turn runs on what the chip says.
    modelLocked:
      Boolean(chatId) && (!switchesModel(chatCaps()) || options.data?.canSwitch === false),
    efforts: choice.efforts,
    effort: choice.effort,
    changeModel,
    changeEffort,
    changeMode,
  };
}

interface ModelSwitchVars {
  chatId: string;
  model: string;
  from: string | null;
}

/** Move an open chat onto a model. The chip reads the pin off the chat list and
 *  the picker's verdicts follow the chat's history, so both are refreshed off
 *  the server's answer through the mutation policy, never patched here: on an
 *  accepted switch, and on a switch refused because the chat moved under the
 *  reader (409). */
function useModelSwitch(chatId: string | null) {
  return useMutation<void, unknown, ModelSwitchVars>({
    mutationFn: async ({ chatId: id, model, from }) => {
      const source = chatData();
      if (!source.setModel) throw new Error("this chat cannot switch models");
      await source.setModel(id, model, undefined, from);
    },
    meta: {
      invalidates: chatId ? [chatKeys.chats(), chatKeys.modelOptions(chatId)] : [chatKeys.chats()],
    },
  });
}

/** An open chat's model picker as its source decides it: every model with
 *  whether the chat may move to it. Read only where the source decides switches
 *  (`modelOptions`) and only for an open chat. */
function useModelOptions(chatId: string | null) {
  const source = chatData();
  return useQuery({
    queryKey: chatKeys.modelOptions(chatId ?? ""),
    enabled: Boolean(chatId) && switchesModel(chatCaps()) && Boolean(source.modelOptions),
    staleTime: 0,
    queryFn: () => {
      if (!chatId || !source.modelOptions) throw new Error("no model options here");
      return source.modelOptions(chatId);
    },
  });
}

/** The composer's transient, dismissable feedback. Command errors cover
 *  ACTIONS that leave no persisted card — a slash command typed with no chat
 *  yet, an RPC/transport failure — since command RESULTS persist as folded
 *  events and need no local state. The models alert re-surfaces when a NEW
 *  failure occurs: its dismiss clears the moment the problem itself clears. */
function useComposerAlerts({
  chatId,
  modelsProblem,
}: {
  chatId: string | null;
  modelsProblem: boolean;
}) {
  const [commandError, setCommandError] = useState<ComposerProblem | null>(null);

  // Reset only on an ACTUAL chat switch; idempotent under StrictMode.
  const prevChatIdRef = useRef(chatId);
  useEffect(() => {
    if (prevChatIdRef.current === chatId) return;
    prevChatIdRef.current = chatId;
    setCommandError(null);
  }, [chatId]);

  const [modelsAlertDismissed, setModelsAlertDismissed] = useState(false);
  useEffect(() => {
    if (!modelsProblem) setModelsAlertDismissed(false);
  }, [modelsProblem]);

  const dismissModelsAlert = useCallback(() => setModelsAlertDismissed(true), []);
  const dismissCommandError = useCallback(() => setCommandError(null), []);
  return {
    commandError,
    setCommandError,
    dismissCommandError,
    modelsAlertDismissed,
    dismissModelsAlert,
  };
}

/** The daemon-served catalogs the composer reads, plus the invalidation
 *  push that keeps them fresh across webviews. */
function useComposerCatalog() {
  const queryClient = useQueryClient();
  // The OpenCode gateway catalog drives the composer's model + effort picker.
  const modelsQuery = useQuery({
    queryKey: chatKeys.models(),
    enabled: servesModels(chatCaps()),
    staleTime: 5 * 60_000,
    // A transient gateway 401 makes listModels() return [] (a "success"); poll
    // while errored OR empty so the catalog self-heals without a reload.
    refetchInterval: refetchWhileErroredOrEmpty,
    queryFn: () => chatData().listModels(),
  });
  const models = useMemo(() => modelsQuery.data ?? [], [modelsQuery.data]);
  // The daemon-served slash-command registry (the CLI's own table).
  const commandsQuery = useQuery({
    queryKey: chatKeys.commands(),
    enabled: chatCaps().opencodeActive,
    // Finite (not Infinity) so the refresh triggers can pick up newly-registered
    // commands; Infinity would keep an always-"fresh" cache that never refetches.
    staleTime: 5 * 60_000,
    refetchInterval: refetchWhileErrored,
    queryFn: () => chatData().listCommands(),
  });
  // The saved Default Chat Model + Effort (resolved against the live catalog).
  // A brand-new chat with no handoff seeds its composer from this; an existing
  // chat keeps its pinned model/effort. Invalidated on `preferencesChanged` so a
  // Settings edit re-seeds the next new chat.
  const chatDefaultsQuery = useQuery({
    queryKey: chatKeys.chatDefaults(),
    enabled: servesModels(chatCaps()),
    staleTime: 5 * 60_000,
    refetchInterval: refetchWhileNoChatDefault,
    queryFn: () => chatData().resolveChatDefaults(),
  });
  useEffect(
    () =>
      chatHost().subscribe((message) => {
        if (message.type !== "preferencesChanged") return;
        void queryClient.invalidateQueries({ queryKey: chatKeys.chatDefaults() });
      }),
    [queryClient],
  );
  const modelOptions = useMemo(
    () => models.map((model) => ({ value: model.id, label: model.displayName })),
    [models],
  );
  return {
    models,
    modelOptions,
    modelsLoading: modelsQuery.isLoading,
    modelsErrored: modelsQuery.isError,
    modelsError: modelsQuery.error,
    modelsReady: modelsQuery.isSuccess,
    daemonCommands: commandsQuery.data ?? [],
    chatDefaults: chatDefaultsQuery.data,
  };
}

/** How many times a mode read that failed for a passing reason is asked again
 *  before the reader is told. */
export const MODE_READ_RETRIES = 6;

/** Whether a failed read is worth asking again on its own: the server fell
 *  over (5xx), asked for a pause (429), or never answered. A refusal with a
 *  reason is an answer, and asking again gets the same one. */
export function readFailurePasses(err: unknown): boolean {
  if (err instanceof TypeError) return true;
  const status = (err as { status?: unknown } | null)?.status;
  return typeof status === "number" && (status >= 500 || status === 429);
}

/** Keep the mode pill on the daemon's truth: seed from the persisted mode on
 *  open/resume, then follow the server-side push. */
function useModeSync({
  chatId,
  setComposerPref,
  setCommandError,
}: {
  chatId: string | null;
  setComposerPref: (chatKey: string, prefs: { mode: string }) => void;
  setCommandError: (problem: ComposerProblem) => void;
}): void {
  // Seed the pill from the chat's PERSISTED permission mode on open/resume.
  // The daemon is the authority (the mode survives in the manifest, and a plan
  // approval can flip it server-side between visits). A failed read leaves the
  // pill on the cached list's value, which can be older than what the agent is
  // actually running under, so the user is told rather than left guessing.
  // Read on every source, not only a harness-backed one: a source that fixes
  // its session's mode (a cloud chat is opened read-only) has to be able to say
  // so, or the chip shows the "default" fallback over a session that refuses
  // every mutation.
  //
  // A read that failed for a passing reason (a database stall answering 503, a
  // backend restarting, the network gone) is asked again on a climbing wait,
  // and at once when the browser comes back online, and the reader is told
  // only if it is still failing after that. Raised on the first 503, the line
  // stood for the life of the page: nothing re-read the mode, so nothing could
  // clear it.
  useEffect(() => {
    if (!chatId) return;
    let cancelled = false;
    let attempt = 0;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const seed = (mode: string | null): void => {
      if (!cancelled && mode) setComposerPref(chatId, { mode });
    };
    const failed = (err: unknown): void => {
      if (cancelled) return;
      if (readFailurePasses(err) && attempt < MODE_READ_RETRIES) {
        retry = setTimeout(read, queryRetryDelay(attempt, err));
        attempt += 1;
        return;
      }
      setCommandError({
        title: "Couldn't read this chat's mode",
        reason: `The mode shown may be stale. ${errorText(err)}`,
      });
    };
    const read = (): void => {
      retry = null;
      void chatData().getPermissionMode(chatId).then(seed, failed);
    };
    const online = (): void => {
      if (retry === null) return;
      clearTimeout(retry);
      read();
    };
    read();
    window.addEventListener("online", online);
    return () => {
      cancelled = true;
      if (retry !== null) clearTimeout(retry);
      window.removeEventListener("online", online);
    };
  }, [chatId, setComposerPref, setCommandError]);

  // Keep the pill live: the daemon pushes `harness.session_state_changed` whenever
  // it flips the mode server-side (a plan approval, an explicit set). Subscribing
  // makes the pill follow the authority; a re-fetch would race the daemon's own
  // commit and could read the mode BEFORE it changed.
  // Subscribed wherever the mode can move at all — a source that fixes it for
  // every session has nothing to push, and one that lets the reader switch
  // (either shell) has to keep the pill on what the session is actually in.
  useEffect(() => {
    if (!chatId || switchableModes(chatCaps()).size === 0) return;
    return chatData().subscribePermissionMode(chatId, (mode) => setComposerPref(chatId, { mode }));
  }, [chatId, setComposerPref]);
}

/** Which model/effort the composer shows: an existing chat's PINNED model (the
 *  picker's catalog only drives brand-new chats), else the handoff's pick,
 *  else the saved default. */
function useModelChoice({
  chatId,
  pinnedModel,
  models,
  composerPrefs,
  handoff,
  chatDefaults,
}: {
  chatId: string | null;
  pinnedModel: Chat["model"];
  models: ModelInfo[];
  composerPrefs: { model?: string; effort?: string } | undefined;
  handoff: ChatHandoff | null;
  chatDefaults: { model?: string | null; effort?: string | null } | undefined;
}) {
  // The pinned effort seeds the composer (override semantics) so the persisted
  // choice survives a resume; user changes persist back via chat.setEffort.
  const pinned = chatId ? pinnedModel : undefined;
  // Where the reader may MOVE an open chat, the picker offers the whole
  // catalogue and the pin is only the seed. Where they may not, the pin IS the
  // list: offering options a source cannot act on is how a chip comes to say a
  // model the chat is not on.
  const switchable = switchesModel(chatCaps());
  const choiceModels: ChoiceModel[] = useMemo(() => {
    if (!pinned?.id || switchable) return models;
    return [
      {
        id: pinned.id,
        efforts: pinned.efforts,
        defaultEffort: models.find((model) => model.id === pinned.id)?.defaultEffort,
      },
    ];
  }, [pinned, models, switchable]);
  const seed = chatId
    ? { model: pinned?.id, effort: pinned?.effort }
    : handoff?.pendingOptions
      ? { model: handoff.pendingOptions.model?.id, effort: handoff.pendingOptions.effort }
      : chatDefaults;
  const choice = resolveModelChoice(choiceModels, seed, composerPrefs);
  return {
    efforts: choice.efforts,
    defaultModel: choice.model || undefined,
    effort: choice.effort,
    reasoningVisible: reasoningIsVisible(choice, chatCaps()),
  };
}

function patchChatListMode(
  queryClient: ReturnType<typeof useQueryClient>,
  chatId: string,
  mode: string,
): void {
  // The cached chat list is the pill's synchronous fallback on the next
  // mount, so a switch the daemon accepted has to land there too.
  const permissionMode = toPermissionMode(mode);
  if (!permissionMode) return;
  const patch = (old?: Chat[]): Chat[] | undefined =>
    old?.map((chat) => (chat.id === chatId ? { ...chat, permissionMode } : chat));
  queryClient.setQueryData<Chat[]>(chatKeys.chats(), patch);
}
