/**
 * The undo stack behind the toast and Cmd+Z.
 *
 * Every destructive Files write answers with the `operation` it performed, and the
 * server can apply that operation's inverse as a new operation. So undo here is not a
 * client-side replay of what the browser thinks happened — it is one call naming the
 * operation id, which is why an undo is correct even for a 40,000-file move the tab
 * never saw finish.
 *
 * The stack is deliberately separate from the toast: the toast is a prompt that fades,
 * the stack is the history. Cmd+Z therefore keeps working long after the toast closed,
 * and Cmd+Shift+Z redoes by undoing the undo — the inverse of an inverse
 * is the original, and the server returns an operation for the undo too.
 *
 * Purges are never pushed: `delete forever` has no inverse, which is why it is the one
 * destructive action that asks for confirmation instead of offering an undo.
 *
 * Two steps the server does NOT name are inverted by the browser instead. A move
 * small enough to run inline answers with the changed node, not with an operation, so
 * there is no id to invert and Cmd+Z after a mis-drop would otherwise do nothing; such
 * a step carries `move`, and the inverse is the opposite move. A rename NEVER answers
 * with an operation at all — `PATCH …/items/{id}` queues only an oversized move — so a
 * rename carries `rename` and the inverse is the old name, put back.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";

import { isOperation, useMoveItem, useRenameItem, useUndoOperation, type Operation } from "@/api/files";
import { FILES_UNDO_DEPTH } from "@/lib/limits";

/** The actions that produce an undoable operation. A purge is not one of them. */
export type UndoableKind = "trash" | "move" | "rename" | "copy" | "restore";

/**
 * A move the server ran inline and answered with the node. There is no operation to
 * invert, so the step is inverted locally: the opposite move, fenced on the version
 * that answer carried. Fencing on the version the move was *asked* at is a guaranteed
 * 412 — a move bumps the node's etag.
 */
export interface InlineMove {
  itemId: string;
  /** The folder the node left: where the inverse puts it back. */
  fromParentId: string;
  /** The folder it landed in: where the opposite direction sends it again. */
  toParentId: string;
  /** The node's version after the move — the inverse's `If-Match`. */
  etag: string;
}

/**
 * A rename, which the server always runs inline and always answers with the node. There
 * is no operation to invert, so the step is inverted locally: the same rename back to
 * the name it had, fenced on the version the rename produced. The version it was asked
 * at is spent — a rename bumps the node's etag.
 */
export interface InlineRename {
  itemId: string;
  /** The name the node had: what the inverse puts back. */
  fromName: string;
  /** The name it now has: where redo sends it again. */
  toName: string;
  /** The node's version after the rename — the inverse's `If-Match`. */
  etag: string;
}

/** One step of history: the operation to invert, and what to call it on screen. */
export interface UndoEntry {
  /** The operation the server performed, and the one `useUndoOperation` inverts.
   *  Absent only for an inline move, which carries {@link UndoEntry.move} instead. */
  operationId?: string;
  driveId: string;
  kind: UndoableKind;
  /** Product copy for the toast, e.g. `Moved 3 items to trash`. */
  label: string;
  /** When the server stops accepting the inverse, from the operation's
   *  `undoableUntil`. Absent means "no stated limit". */
  undoableUntil?: string;
  /** The version of the row the write acted on, as the write knew it — what the
   *  undo request names as `If-Match`. Every Files mutation must name one, and an
   *  undo that named none was refused (428) and never ran. */
  etag?: string;
  /** Set when the write named no operation because it ran inline: the step is
   *  inverted by issuing the opposite move rather than by naming an id. */
  move?: InlineMove;
  /** Set on a rename, which the server never answers with an operation: the step
   *  is inverted by renaming back rather than by naming an id. */
  rename?: InlineRename;
}

/** A write that landed, as the surface that made it reports it. Structural on
 *  purpose: the Files page, the chat's file explorer and anything else that
 *  writes hand the same shape to {@link undoStep}. */
export interface LandedWrite {
  kind: UndoableKind;
  driveId: string;
  /** The operation the server named, when it named one. */
  operationId?: string;
  /** What the server says about that operation: whether it recorded an inverse
   *  the undo route will apply. */
  undoable?: boolean;
  undoableUntil?: string;
  label: string;
  items: readonly { id: string; etag: string }[];
  /** The node as the answer returned it, when the write ran inline. */
  node?: { id: string; etag: string; parentId?: string | null };
  /** The folder the rows left, for a move the browser inverts itself. */
  fromParentId?: string;
}

/**
 * The history step a landed write earns, or `null` when it earns none.
 *
 * Undo must only ever promise what the server will do. A move small enough to
 * run inline names no operation but is still invertible here — the opposite
 * move. Everything else is invertible only if the server recorded an inverse
 * for it, which it says on the operation: a copy records none, so offering
 * Cmd+Z after one was a promise that did nothing when it was kept.
 */
export function undoStep(write: LandedWrite): UndoEntry | null {
  const landed = write.node;
  if (write.kind === "move" && write.operationId === undefined && landed) {
    if (write.fromParentId === undefined) return null;
    return {
      driveId: write.driveId,
      kind: "move",
      label: write.label,
      move: {
        itemId: landed.id,
        fromParentId: write.fromParentId,
        toParentId: landed.parentId ?? "",
        etag: landed.etag,
      },
    };
  }
  if (write.operationId === undefined || write.undoable !== true) return null;
  const etag = write.node?.etag ?? write.items[0]?.etag;
  return {
    operationId: write.operationId,
    driveId: write.driveId,
    kind: write.kind,
    label: write.label,
    ...(write.undoableUntil === undefined ? {} : { undoableUntil: write.undoableUntil }),
    // The version the write acted on: the node as the answer returned it when
    // there was one, else the row as the write knew it. The undo request names
    // it as If-Match — without one the server refused.
    ...(etag === undefined ? {} : { etag }),
  };
}

export interface UndoState {
  /** Newest last: the top of the stack is what Cmd+Z acts on. */
  past: UndoEntry[];
  /** Newest last: the top is what Cmd+Shift+Z acts on. */
  future: UndoEntry[];
}

export const EMPTY_UNDO_STATE: UndoState = { past: [], future: [] };

/** How many steps back the stack remembers. Deep enough that a burst of moves stays
 *  reversible, bounded so a long session cannot grow without limit. */
export const UNDO_DEPTH = FILES_UNDO_DEPTH;

export type UndoAction =
  /** A destructive write succeeded; its operation becomes the newest undoable step. */
  | { type: "push"; entry: UndoEntry }
  /** The top of `past` was inverted; the inverse operation becomes redoable. */
  | { type: "undone"; entry: UndoEntry }
  /** The top of `future` was inverted back; it becomes undoable again. */
  | { type: "redone"; entry: UndoEntry }
  | { type: "clear" };

/**
 * The pure history transition.
 *
 * `push` clears the redo branch, the standard rule: once new work happens, the
 * abandoned branch can no longer be replayed onto a tree that has moved on.
 */
export function undoReducer(state: UndoState, action: UndoAction): UndoState {
  switch (action.type) {
    case "push":
      return {
        past: [...state.past, action.entry].slice(-UNDO_DEPTH),
        future: [],
      };
    case "undone": {
      if (state.past.length === 0) return state;
      return {
        past: state.past.slice(0, -1),
        // The redo step inverts the operation the UNDO produced, not the original.
        future: [...state.future, action.entry].slice(-UNDO_DEPTH),
      };
    }
    case "redone": {
      if (state.future.length === 0) return state;
      return {
        past: [...state.past, action.entry].slice(-UNDO_DEPTH),
        future: state.future.slice(0, -1),
      };
    }
    case "clear":
      return EMPTY_UNDO_STATE;
  }
}

/** The step Cmd+Z would act on, or undefined when there is nothing to undo. */
export function topOfPast(state: UndoState): UndoEntry | undefined {
  return state.past[state.past.length - 1];
}

/** The step Cmd+Shift+Z would act on. */
export function topOfFuture(state: UndoState): UndoEntry | undefined {
  return state.future[state.future.length - 1];
}

/** Whether a keyboard event is undo, redo, or neither — on either platform.
 *  Cmd+Shift+Z and Ctrl+Y both redo; Ctrl+Z with Shift is redo, without is undo. */
export function undoShortcutFor(event: {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
}): "undo" | "redo" | null {
  if (event.altKey) return null;
  const accel = event.metaKey || event.ctrlKey;
  if (!accel) return null;
  const key = event.key.toLowerCase();
  if (key === "y" && !event.metaKey) return "redo";
  if (key !== "z") return null;
  return event.shiftKey ? "redo" : "undo";
}

/** A field the person is typing in must keep its own undo. */
function isTextEntry(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

export interface UndoController {
  state: UndoState;
  /** Record a completed destructive write. */
  push: (entry: UndoEntry) => void;
  /** Invert the top of the stack. Resolves once the server accepted the inverse. */
  undo: () => Promise<void>;
  /** Invert the inverse. */
  redo: () => Promise<void>;
  /** Forget the history — a drive switch, a sign-out. */
  clear: () => void;
  canUndo: boolean;
  canRedo: boolean;
  /** What a toast would name, if one is showing. */
  pending: UndoEntry | undefined;
  /** True while an inverse is in flight, so the toast can disable its button. */
  running: boolean;
}

/**
 * What tells one step from the next, for a surface that must notice a new one.
 *
 * A locally-inverted step names no operation, so it is keyed by the node and the
 * version its write produced — which is precisely what changes when the next step
 * arrives. Keying on `operationId` alone would leave every such step sharing the same
 * empty identity, and a prompt that thinks it already showed this one never opens.
 */
export function entryKey(entry: UndoEntry | undefined): string | null {
  if (!entry) return null;
  if (entry.operationId !== undefined) return entry.operationId;
  if (entry.move) return `move:${entry.move.itemId}:${entry.move.etag}`;
  return entry.rename ? `rename:${entry.rename.itemId}:${entry.rename.etag}` : null;
}

/** Product copy for the inverse of a step, so the redo toast reads correctly. */
export function inverseLabel(entry: UndoEntry): string {
  switch (entry.kind) {
    case "trash":
      return "Restored from trash";
    case "restore":
      return "Moved back to trash";
    case "move":
      return "Move undone";
    case "rename":
      return "Rename undone";
    case "copy":
      return "Copy undone";
  }
}

/**
 * The stack, the Cmd+Z binding and the mutation, in one hook.
 *
 * The window listener is what makes "Cmd+Z after the toast closed" work: the shortcut
 * is bound to the page, not to the toast's DOM, so closing the toast changes nothing
 * about what the keystroke does.
 */
export function useUndoStack(): UndoController {
  const [state, dispatch] = useReducer(undoReducer, EMPTY_UNDO_STATE);
  const undoOperation = useUndoOperation();
  // The inverse of an inline move goes through the same mutation the browser moves
  // with, so the row leaves and arrives the way any move does and a refusal renders
  // the same sentence rather than a second, private error path.
  const moveItem = useMoveItem();
  // Likewise for a rename: the inverse goes through the mutation the row renames
  // with, so the name changes on screen the way it does for any rename and a
  // refusal — a lease, a name already taken — renders the same sentence.
  const renameItem = useRenameItem();

  const push = useCallback((entry: UndoEntry) => dispatch({ type: "push", entry }), []);
  const clear = useCallback(() => dispatch({ type: "clear" }), []);

  /** Invert one step and record the operation that inverting produced, so the opposite
   *  direction has something real to act on. */
  const invert = useCallback(
    async (entry: UndoEntry | undefined, done: "undone" | "redone"): Promise<void> => {
      if (!entry) return;
      const local = entry.move;
      if (local) {
        const result = await moveItem.mutateAsync({
          driveId: entry.driveId,
          itemId: local.itemId,
          etag: local.etag,
          parentId: local.fromParentId,
        });
        dispatch({
          type: done,
          entry: isOperation(result)
            ? {
                // The inverse was big enough to queue, so from here on the step is
                // server-named like any other and inverts by id.
                driveId: entry.driveId,
                operationId: result.id,
                kind: entry.kind,
                label: inverseLabel(entry),
                undoableUntil: result.undoableUntil ?? undefined,
              }
            : {
                driveId: entry.driveId,
                kind: entry.kind,
                label: inverseLabel(entry),
                move: {
                  itemId: local.itemId,
                  // The opposite direction sends it back where it just came from,
                  // fenced on the version this move produced.
                  fromParentId: local.toParentId,
                  toParentId: local.fromParentId,
                  etag: result.etag,
                },
              },
        });
        return;
      }
      const naming = entry.rename;
      if (naming) {
        const result = await renameItem.mutateAsync({
          driveId: entry.driveId,
          itemId: naming.itemId,
          etag: naming.etag,
          name: naming.fromName,
        });
        // A rename is answered with the node; the 202 shape belongs to an oversized
        // move and can never arrive here, so there is no version to fence the
        // opposite direction on and the step stops being reversible.
        if (isOperation(result)) return;
        dispatch({
          type: done,
          entry: {
            driveId: entry.driveId,
            kind: entry.kind,
            label: inverseLabel(entry),
            rename: {
              itemId: naming.itemId,
              // The opposite direction puts back the name this rename just took
              // away, fenced on the version this rename produced.
              fromName: naming.toName,
              toName: naming.fromName,
              etag: result.etag,
            },
          },
        });
        return;
      }
      if (entry.operationId === undefined) return;
      const operation: Operation = await undoOperation.mutateAsync({
        driveId: entry.driveId,
        operationId: entry.operationId,
        etag: entry.etag,
      });
      dispatch({
        type: done,
        entry: {
          driveId: entry.driveId,
          operationId: operation.id,
          kind: entry.kind,
          label: inverseLabel(entry),
          undoableUntil: operation.undoableUntil ?? undefined,
        },
      });
    },
    [undoOperation, moveItem, renameItem],
  );

  const undo = useCallback(() => invert(topOfPast(state), "undone"), [invert, state]);
  const redo = useCallback(() => invert(topOfFuture(state), "redone"), [invert, state]);

  /** Whether an inverse is on the wire right now.
   *
   *  A ref, not `running`: that is React state, and the keystrokes this guards
   *  against all arrive before the render that would set it. */
  const inFlight = useRef(false);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isTextEntry(event.target)) return;
      // One step per press, the way Finder behaves: a held Cmd+Z undoes the top
      // of the stack and stops there rather than unwinding the whole history.
      if (event.repeat) return;
      const which = undoShortcutFor(event);
      if (which === null) return;
      // The history only moves once the server has accepted the inverse, so
      // every press before then reads the SAME top of the stack — a burst would
      // send one operation's undo N times, or N PATCHes fenced on one etag, and
      // the losers come back 412 as refusals the person never caused.
      if (inFlight.current) return;
      const entry = which === "undo" ? topOfPast(state) : topOfFuture(state);
      if (!entry) return;
      event.preventDefault();
      inFlight.current = true;
      void invert(entry, which === "undo" ? "undone" : "redone")
        // A refused inverse renders through the mutation that made it, as it
        // does from the toast's button; all the key needs is to be usable again.
        .catch(() => {})
        .finally(() => {
          inFlight.current = false;
        });
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [invert, state]);

  return useMemo(
    () => ({
      state,
      push,
      undo,
      redo,
      clear,
      canUndo: state.past.length > 0,
      canRedo: state.future.length > 0,
      pending: topOfPast(state),
      running: undoOperation.isPending || moveItem.isPending || renameItem.isPending,
    }),
    [
      state,
      push,
      undo,
      redo,
      clear,
      undoOperation.isPending,
      moveItem.isPending,
      renameItem.isPending,
    ],
  );
}
