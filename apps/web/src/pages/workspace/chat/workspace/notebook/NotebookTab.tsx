// The "Notebook" view of a `.alknb.py` file: the open notebook editor wired to
// the platform. The document is the live Loro notebook (one channel per
// notebook per browser tab), each cell's editor a binding on its cell's text,
// the engine's state a fold over the notebook channel's events, and every
// action a notebook route. Loaded only when a notebook is opened.

import {
  ABSENT_KERNEL,
  NotebookEditor,
  installLine,
  platformModuleLoader,
  type FrameServices,
  type NotebookActions,
  type NotebookOutputSettings,
  type Presence,
  type RunTarget,
  type SqlConnectionSlot,
} from "@alkera/notebook-ui";
import "@alkera/notebook-ui/styles";
import { Button, ConfirmDialog } from "@alkera/ui";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { useUserScope } from "@/api/auth";
import { apiBaseUrl } from "@/api/client";
import { refusalSentence } from "@/api/errors";
import { keys } from "@/api/keys";
import {
  getTablePage,
  outputBlobUrl,
  postComm,
  postEnvChange,
  postEnvInstall,
  postClearOutputs,
  postKernel,
  postRun,
  NOTEBOOK_WAKING,
  untilAwake,
  useEnvPackages,
  useNotebookConnections,
  useNotebookEnvs,
  useNotebookView,
  type NotebookKernelAction,
  type WakeWait,
} from "@/api/notebooks";
import { toBase64 } from "@/api/realtime/crdt/bytes";
import { EDITS_NOT_KEPT, NO_ANSWER, type LiveSocket } from "@/api/realtime/crdt/channel";
import type { LoroApi } from "@/api/realtime/crdt/loro";
import { getRealtimeClient } from "@/api/realtime/client";
import { hueOf } from "@/lib/personHue";

import type { FileViewProps } from "../fileViewers";
import { LiveEditsNotice } from "../LiveEditsNotice";
import { ReadOnlyFileText } from "../ReadOnlyFileText";
import { usePaneWidth } from "../usePaneWidth";
import { cellHash, useRevealedCell } from "./cellAnchor";
import { acquireLiveNotebook, type LiveNotebookState } from "./liveNotebook";
import { loroCellText } from "./loroPorts";
import { NotebookCarets } from "./notebookCarets";
import { useHostDark } from "../../useHostDark";
import { NotebookComms, routeTransport } from "./notebookComms";
import { SqlConnectionPicker } from "./SqlConnectionPicker";
import { NotebookRuntimeStore, envMayHaveChanged, followInstall, followInterrupt, followPresence } from "./notebookRuntime";
import { kernelEventOfView, sortParam } from "./notebookWire";

import "./notebookTab.css";
import "./outputRenderers";


/** Below this width the editor is one column with no command mode. */
export const NARROW_PX = 768;

/** What the tab says while it cannot show the notebook live. */
export const OPENING = "Opening the notebook";
export const NOT_LIVE = "This notebook can't be opened live here. Its source is shown read only.";

/** Why the server would not open the file as a notebook, by the reader's
 *  read-only reason. Each is shown above the file's text, read only. */
export const NOT_A_NOTEBOOK = "This file isn't a notebook, so it's shown read only. Fix it in Source to open it as one.";
export const UNREADABLE = "This notebook can't be read, so it's shown read only. Fix it in Source to open it again.";
export const NEWER_FORMAT = "This notebook was saved by a newer version, so it's shown read only.";

/** What the tab says when it cannot show the notebook live. */
export function notLiveSentence(detail: string | undefined): string {
  if (detail === "not_a_notebook") return NOT_A_NOTEBOOK;
  if (detail === "unreadable") return UNREADABLE;
  if (detail === "newer_format") return NEWER_FORMAT;
  return NOT_LIVE;
}

/** What the tab says when the server never answered its opening. */
export const NOT_OPENED = "The notebook didn't open. The server didn't answer.";

/** The channel the engine's events for a notebook arrive on. */
export function notebookChannel(itemId: string): string {
  return `nb:${itemId}`;
}

const ASSET = /^alkera-asset:sha256:([0-9a-f]{64})$/;

/** What a request that needs the machine says when it was still waking at
 *  the end of the wait. */
const NOT_WOKEN = "The machine did not wake. Try again.";

/** Why a request that needs the notebook's machine did not go through: the
 *  server's own sentence where it wrote one (a machine that is gone, a start
 *  the org's credit refused), else `fallback`. */
function kernelRefusal(error: unknown, fallback: string): string {
  return refusalSentence(error, { known: { [NOTEBOOK_WAKING]: NOT_WOKEN }, fallback });
}

/** A widget asset the notebook's kernel offered, by its content hash. The
 *  frame verifies the hash; anything not named by hash is refused here. */
export async function widgetAsset(driveId: string, itemId: string, name: string, fetchImpl: typeof fetch = fetch): Promise<string> {
  const sha = ASSET.exec(name)?.[1];
  if (sha === undefined) throw new Error(`The module ${name} is not available.`);
  const url = `${apiBaseUrl}/api/v1/notebooks/${encodeURIComponent(driveId)}/${encodeURIComponent(itemId)}/widget-assets/${sha}`;
  const response = await fetchImpl(url, { credentials: "include" });
  if (!response.ok) throw new Error(`The widget asset could not be loaded (${response.status}).`);
  return response.text();
}

function clientRunId(): string {
  return globalThis.crypto.randomUUID().replace(/-/g, "").slice(0, 24);
}

export interface NotebookTabProps extends FileViewProps {
  /** Seams for tests: the socket, Loro, and the request function. */
  socket?: LiveSocket;
  loadLoro?: () => Promise<LoroApi>;
  fetchImpl?: typeof fetch;
}

export default function NotebookTab({ tab, ctx, item, onEdit, socket, loadLoro, fetchImpl }: NotebookTabProps) {
  const itemId = tab.node_id ?? item.id;
  const driveId = ctx.driveId;
  const live = useMemo(() => socket ?? ctx.liveSocket ?? getRealtimeClient(), [socket, ctx.liveSocket]);
  const account = useUserScope();
  const [state, setState] = useState<LiveNotebookState>({ kind: "pending" });
  /** Bumped by Retry: a notebook that fell back is opened afresh. */
  const [reopen, setReopen] = useState(0);
  const [canWrite, setCanWrite] = useState(false);
  /** The reader dismissed the cells a fallen-back notebook offered back. */
  const [lostDismissed, setLostDismissed] = useState(false);
  /** A link an output asked to open, waiting for the reader to say yes. */
  const [link, setLink] = useState<string | null>(null);
  const pane = useRef<HTMLDivElement>(null);
  const width = usePaneWidth(pane);
  const store = useMemo(() => new NotebookRuntimeStore(), []);
  const comms = useMemo(
    () =>
      new NotebookComms(
        (message) =>
          void postComm(driveId, itemId, message, fetchImpl).catch(() =>
            store.patch({ notice: { id: `comm-${Date.now()}`, message: "A widget change did not reach the kernel.", tone: "warning" } }),
          ),
        routeTransport(driveId, itemId, () => live.peerId, fetchImpl),
      ),
    [driveId, itemId, fetchImpl, store, live],
  );
  const runtime = useSyncExternalStore(store.subscribe, () => store.snapshot);
  const view = useNotebookView(driveId, itemId, state.kind === "live");
  const connections = useNotebookConnections(driveId, itemId, state.kind === "live", fetchImpl);
  const connectionList = connections.data?.connections;
  const sqlConnection = useCallback(
    (slot: SqlConnectionSlot) => <SqlConnectionPicker slot={slot} connections={connectionList} />,
    [connectionList],
  );
  const queryClient = useQueryClient();
  /** Targets of runs the engine asked to confirm, to send again confirmed. */
  const pendingRuns = useRef(new Map<string, RunTarget>());
  const stopInterrupt = useRef<(() => void) | null>(null);
  const stopInstall = useRef<(() => void) | null>(null);
  useEffect(
    () => () => {
      stopInterrupt.current?.();
      stopInstall.current?.();
    },
    [],
  );
  /** Ask the engine for the environment and its packages again. */
  const refreshEnv = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: keys.notebooks.envs(driveId, itemId) });
    void queryClient.invalidateQueries({ queryKey: keys.notebooks.packagesOf(driveId, itemId) });
  }, [queryClient, driveId, itemId]);

  useEffect(() => {
    // Never before the reader is known: what the notebook keeps for the next
    // page is filed under them, and one opened for nobody would keep nothing.
    if (account === null) return;
    const { notebook, release } = acquireLiveNotebook(itemId, { socket: live, account, ...(loadLoro ? { loadLoro } : {}) });
    const stop = notebook.subscribe(setState);
    return () => {
      stop();
      release();
    };
  }, [account, itemId, live, loadLoro, reopen]);

  const liveState = state.kind === "live" ? state : null;

  // A notebook needs the chat's machine running: opening one asks for the
  // same wake opening the chat does, and so does a run.
  const wake = ctx.wake;
  useEffect(() => {
    wake?.();
  }, [wake]);

  useEffect(() => {
    if (liveState === null) return;
    setCanWrite(liveState.channel.canWrite);
    return liveState.channel.listen({
      writable: setCanWrite,
      // The server refused an edit, or a restarted history could not keep
      // it: the cells it took are offered back, never dropped unseen.
      notice: (notice) => {
        if (notice === null) return;
        store.patch({
          notice: { id: `live-${Date.now()}`, message: notice.message, tone: "warning", ...(notice.restorable ? { restorable: notice.restorable } : {}) },
        });
      },
    });
  }, [liveState, store]);

  // The engine's events: a snapshot on join, then each event in order.
  useEffect(() => {
    if (liveState === null) return;
    const channel = notebookChannel(itemId);
    const unsubscribe = live.subscribe(channel);
    const stop = live.onFrame((frame) => {
      if (frame.t !== "nb" || frame.channel !== channel) return;
      // A resync with no view: the server let go of the channel and its
      // snapshot comes back on subscribing again.
      if (frame.event.type === "resync" && frame.event.view === undefined) {
        live.send({ t: "subscribe", channel });
        return;
      }
      // Widget traffic goes to the frames; everything else is state.
      if (comms.handle(frame.event)) return;
      const before = store.snapshot;
      store.dispatch(frame.event);
      if (envMayHaveChanged(before, store.snapshot, frame.event)) refreshEnv();
    });
    return () => {
      stop();
      unsubscribe();
    };
  }, [itemId, live, liveState, store, comms, refreshEnv]);

  // The outputs the view lists, for cells the channel has shown none of.
  // Heard when it was fetched, not when it is shown: a cached view read
  // again must not bring back agents whose claims have lapsed since.
  useEffect(() => {
    if (view.data) store.dispatch({ type: "view", view: view.data, received_at: new Date(view.dataUpdatedAt).toISOString() });
  }, [view.data, view.dataUpdatedAt, store]);

  // Each agent the server placed in a cell goes when the server said it would.
  useEffect(() => followPresence(store), [store]);

  // The view's kernel, until the channel says more. A view with no kernel
  // (`absent`) says nothing over a kernel the channel already reported.
  useEffect(() => {
    const kernel = view.data?.kernel;
    if (!kernel) return;
    if ((kernel.kernel_id ?? null) === null && store.snapshot.kernelId !== null) return;
    store.dispatch(kernelEventOfView(kernel));
  }, [view.data, store]);

  // What needs the notebook's machine waits while the server wakes the chat
  // holding its folder; the kernel chip says so while anything waits.
  const wakers = useRef(0);
  const wakeWait = useMemo<WakeWait>(
    () => ({
      onWaking: (waking) => {
        wakers.current += waking ? 1 : -1;
        store.patch({ waking: wakers.current > 0 });
      },
    }),
    [store],
  );

  // The environment is asked of the engine's own listing: the view names it
  // only while a kernel runs.
  const envs = useNotebookEnvs(driveId, itemId, state.kind === "live", fetchImpl, wakeWait);
  useEffect(() => {
    const listing = envs.data;
    if (!listing) return;
    store.dispatch({ type: "env.state", env: listing.current, envs: listing.envs ?? [], shared: listing.shared ?? true });
  }, [envs.data, store]);
  const envId = state.kind === "live" ? (runtime.kernel.env?.env_id ?? null) : null;
  const packages = useEnvPackages(driveId, itemId, envId, fetchImpl, wakeWait);
  useEffect(() => {
    if (packages.data) store.dispatch({ type: "env.state", packages: packages.data.packages ?? [], requirements: packages.data.requirements ?? [] });
  }, [packages.data, store]);

  const carets = useMemo(
    () => (liveState === null ? null : new NotebookCarets(liveState.channel, liveState.loro, hueOf)),
    [liveState],
  );
  useEffect(() => () => carets?.dispose(), [carets]);
  const subscribePresence = useCallback((l: () => void) => carets?.subscribe(l) ?? (() => {}), [carets]);
  const presenceVersion = useSyncExternalStore(subscribePresence, () => carets?.presence() ?? EMPTY);
  const text = useMemo(
    () => (liveState === null || carets === null ? null : loroCellText({ channel: liveState.channel, loro: liveState.loro, doc: liveState.doc, carets, hueOf })),
    [liveState, carets],
  );

  // Keyed on the URL, not the view: a refreshed view must not rebuild every
  // framed output on the page.
  const frameUrl = view.data?.output_frame_url;
  const url = typeof frameUrl === "string" && frameUrl !== "" ? frameUrl : null;
  const frame = useMemo<FrameServices | undefined>(() => {
    if (url === null) return undefined;
    return {
      bootstrapUrl: url,
      loadModule: platformModuleLoader((name) => widgetAsset(driveId, itemId, name, fetchImpl)),
      confirmLink: (href) => setLink(href),
      comms: comms.bridge(),
    };
  }, [url, driveId, itemId, fetchImpl, comms]);

  // The notebook is drawn in the palette the app around it is in, and moves
  // with it: the editor, its outputs and every framed output take this theme.
  const dark = useHostDark();
  const output = useMemo<NotebookOutputSettings>(
    () => ({
      theme: dark ? "dark" : "light",
      ...(frame ? { frame } : {}),
      blobUrl: (sha: string) => outputBlobUrl(driveId, itemId, sha),
    }),
    [dark, frame, driveId, itemId],
  );

  const actions = useMemo<NotebookActions>(() => {
    const frontier = (): string | null => {
      const doc = liveState?.doc.loroDoc;
      return doc ? toBase64(doc.oplogVersion().encode()) : null;
    };
    const run = (target: RunTarget, confirm = false) => {
      wake?.();
      // One body for every ask: the run id keeps a run asked again while the
      // machine wakes one run.
      const body = { target, frontier: frontier(), confirm_expensive: confirm, client_run_id: clientRunId() };
      void untilAwake(() => postRun(driveId, itemId, body, fetchImpl), wakeWait)
        .then((accepted) => {
          store.noteRun(accepted.run_id, target);
          if (accepted.status === "needs_confirmation") pendingRuns.current.set(accepted.run_id, target);
        })
        .catch((error: unknown) =>
          store.patch({ notice: { id: `run-${Date.now()}`, message: kernelRefusal(error, "The run could not be started."), tone: "danger" } }),
        );
    };
    const kernel = (action: NotebookKernelAction) =>
      void untilAwake(() => postKernel(driveId, itemId, action, fetchImpl), wakeWait).catch((error: unknown) => {
        // The request never reached the kernel, so no interrupt is under
        // way: the toolbar must not go on saying one is.
        stopInterrupt.current?.();
        stopInterrupt.current = null;
        store.patch({
          kernel: { ...store.snapshot.kernel, interrupt: "none" },
          notice: { id: `kernel-${Date.now()}`, message: kernelRefusal(error, "The kernel did not take that request."), tone: "danger" },
        });
      });
    return {
      run: (target) => run(target),
      confirmRun: (runId) => {
        const target = pendingRuns.current.get(runId);
        pendingRuns.current.delete(runId);
        store.patch({ confirmation: null });
        if (target) run(target, true);
      },
      cancelRun: (runId) => {
        pendingRuns.current.delete(runId);
        store.patch({ confirmation: null });
      },
      kernel: (action) => {
        if (action === "interrupt" || action === "interrupt_all") {
          stopInterrupt.current?.();
          stopInterrupt.current = followInterrupt(store);
        }
        kernel(action);
      },
      clearOutputs: (cellIds) => {
        void untilAwake(() => postClearOutputs(driveId, itemId, cellIds, fetchImpl), wakeWait).catch((error: unknown) =>
          store.patch({ notice: { id: `clear-${Date.now()}`, message: kernelRefusal(error, "The outputs could not be cleared."), tone: "danger" } }),
        );
      },
      switchEnv: (recorded) => {
        // Checked and committed first; the kernel restarts only once the
        // notebook names an environment the server will accept.
        try {
          liveState?.doc.apply([{ op: "set_setting", key: "env", value: recorded }]);
        } catch {
          store.patch({ notice: { id: `env-${Date.now()}`, message: "The environment could not be switched.", tone: "danger" } });
          return;
        }
        kernel("restart");
      },
      install: (packages) => {
        // The request only says the install was accepted; how it went
        // arrives on the notebook channel, and until then it is under way.
        stopInstall.current?.();
        stopInstall.current = followInstall(store, packages, refreshEnv);
        void untilAwake(() => postEnvInstall(driveId, itemId, packages, fetchImpl), wakeWait).catch((error: unknown) => {
          const install = { status: "error" as const, packages, message: refusalSentence(error, { fallback: "" }) || null };
          store.patch({ installing: false, install, notice: { id: `install-${Date.now()}`, message: installLine(install), tone: "danger" } });
        });
      },
      changeEnv: (action, packages) => {
        stopInstall.current?.();
        stopInstall.current = followInstall(store, packages, refreshEnv, undefined, action);
        void untilAwake(() => postEnvChange(driveId, itemId, action, packages, fetchImpl), wakeWait).catch((error: unknown) => {
          const install = { status: "error" as const, action, packages, message: refusalSentence(error, { fallback: "" }) || null };
          store.patch({ installing: false, install, notice: { id: `install-${Date.now()}`, message: installLine(install), tone: "danger" } });
        });
      },
      inspectFrame: async (query) => {
        if (!query.cell_id) throw new Error("This table can't be paged here.");
        // The table page as the server sent it: the table reads it as it
        // reads its output's first page.
        return getTablePage(
          driveId,
          itemId,
          query.cell_id,
          { offset: query.offset, limit: query.limit, sort: sortParam(query.sort), filter_sql: query.filter_sql ?? null },
          fetchImpl,
        );
      },
      copyCellLink: (cellId) => {
        const url = new URL(window.location.href);
        url.hash = cellHash(cellId);
        void navigator.clipboard?.writeText(url.toString());
      },
      dismissNotice: () => store.patch({ notice: null }),
    };
  }, [driveId, itemId, fetchImpl, liveState, store, refreshEnv, wake, wakeWait]);

  const presence = useMemo(
    (): Presence[] =>
      presenceVersion.map((p): Presence => ({
        id: p.peer,
        kind: "person",
        display_name: p.stamp.displayName.trim() || p.stamp.email,
        hue: hueOf({ userId: p.stamp.userId, email: p.stamp.email }),
        cell_id: p.cellId,
      })).concat(
        runtime.agents.list.map(
          (p): Presence => ({
            id: p.actor,
            kind: "agent",
            display_name: p.display_name,
            acting_for: p.acting_for,
            hue: hueOf(p.actor),
            cell_id: p.cell_id,
          }),
        ),
      ),
    [presenceVersion, runtime.agents],
  );

  const revealCell = useRevealedCell();

  return (
    <div ref={pane} className="alk-nb-tab" data-state={state.kind}>
      {state.kind === "pending" ? (
        <p className="alk-nb-tab__note" role="status">
          {OPENING}
        </p>
      ) : state.kind === "fallback" ? (
        <>
          {state.reason === NO_ANSWER ? (
            <div className="alk-nb-tab__note" role="alert">
              <p>{NOT_OPENED}</p>
              <Button size="sm" variant="secondary" fill="outline" onClick={() => setReopen((n) => n + 1)}>
                Retry
              </Button>
            </div>
          ) : (
            <p className="alk-nb-tab__note" role="alert">
              {notLiveSentence(state.detail)}
            </p>
          )}
          {state.unacknowledged && !lostDismissed ? (
            <LiveEditsNotice message={EDITS_NOT_KEPT} restorable={state.unacknowledged} onDismiss={() => setLostDismissed(true)} />
          ) : null}
          <div className="alk-nb-tab__source">
            <ReadOnlyFileText driveId={driveId} item={item} />
          </div>
        </>
      ) : text !== null ? (
        <NotebookEditor
          doc={state.doc}
          text={text}
          runtime={{
            ...runtime,
            kernel: runtime.kernel ?? ABSENT_KERNEL,
            presence,
            settings: view.data ? { values: view.data.settings, sources: view.data.settings.sources ?? {} } : null,
          }}
          actions={actions}
          permissions={{ canEdit: canWrite, canRun: canWrite }}
          output={output}
          narrow={width > 0 && width < NARROW_PX}
          initialCellId={revealCell?.id ?? null}
          revealCell={revealCell}
          sqlConnection={sqlConnection}
          onEdit={onEdit}
        />
      ) : null}
      <ConfirmDialog
        open={link !== null}
        title="Open this link from an output?"
        consequence={link ?? ""}
        confirmLabel="Open link"
        onClose={() => setLink(null)}
        onConfirm={() => {
          if (link !== null) window.open(link, "_blank", "noopener,noreferrer");
          setLink(null);
        }}
      />
    </div>
  );
}

const EMPTY: never[] = [];
