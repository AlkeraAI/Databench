// One cell's CodeMirror editor.
//
// The view is made once per binding and kept: a kind change swaps the
// language, a permission change swaps read-only, and the binding keeps the
// text in step with the document. The notebook's own keys (run, leave the
// editor, find) sit above everything CodeMirror brings, so Shift+Enter runs
// rather than inserting a line.

import { toggleComment } from "@codemirror/commands";
import { Compartment, EditorState, Prec, type Extension } from "@codemirror/state";
import { EditorView, keymap, type KeyBinding } from "@codemirror/view";
import { useEffect, useRef } from "react";

import type { CellKind } from "../model/types";
import type { CommandId } from "./commands";
import { cellBasics, languageFor } from "./languages";
import type { CellTextBinding } from "./ports";

export interface CellEditorProps {
  binding: CellTextBinding;
  kind: CellKind;
  readOnly: boolean;
  /** Edit-mode keys: CodeMirror key spec to command. */
  keys: readonly { spec: string; id: CommandId }[];
  onCommand(id: CommandId, view: EditorView): void;
  onFocus(): void;
  /** Called with the view once it exists, and with null when it goes. */
  onView?(view: EditorView | null): void;
  /** Extra extensions from the host (caret drawing). */
  extensions?: Extension;
}

/** A table spec (`Mod-Shift-Minus`) as CodeMirror spells it (`Mod-Shift--`). */
export function codeMirrorKey(spec: string): string {
  return spec.replace(/Minus$/, "-");
}

export function CellEditor({ binding, kind, readOnly, keys, onCommand, onFocus, onView, extensions }: CellEditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const language = useRef(new Compartment());
  const writable = useRef(new Compartment());
  const commandKeys = useRef(new Compartment());
  // The latest callbacks, read by the keymap without remaking the view.
  const latest = useRef({ onCommand, onFocus });
  latest.current = { onCommand, onFocus };

  useEffect(() => {
    const parent = host.current;
    if (parent === null) return;
    const created = new EditorView({
      parent,
      state: EditorState.create({
        doc: binding.text(),
        extensions: [
          binding.extension,
          language.current.of([languageFor(kind), cellBasics(kind)]),
          writable.current.of(readOnlyExtension(readOnly)),
          commandKeys.current.of(keyExtension(keys, latest)),
          keymap.of([{ key: "Mod-/", run: toggleComment }]),
          EditorView.domEventHandlers({ focus: () => latest.current.onFocus() }),
          extensions ?? [],
        ],
      }),
    });
    view.current = created;
    onView?.(created);
    return () => {
      onView?.(null);
      created.destroy();
      view.current = null;
    };
    // The view lives as long as its binding; the rest is reconfigured below.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the view is rebuilt only for a new binding
  }, [binding]);

  useEffect(() => {
    view.current?.dispatch({ effects: language.current.reconfigure([languageFor(kind), cellBasics(kind)]) });
  }, [kind]);

  useEffect(() => {
    view.current?.dispatch({ effects: writable.current.reconfigure(readOnlyExtension(readOnly)) });
  }, [readOnly]);

  useEffect(() => {
    view.current?.dispatch({ effects: commandKeys.current.reconfigure(keyExtension(keys, latest)) });
  }, [keys]);

  return <div ref={host} className="nb-cell-editor" data-kind={kind} />;
}

function readOnlyExtension(readOnly: boolean): Extension {
  return [EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)];
}

function keyExtension(
  keys: readonly { spec: string; id: CommandId }[],
  latest: { current: { onCommand(id: CommandId, view: EditorView): void } },
): Extension {
  const bindings: KeyBinding[] = keys
    .filter(({ id }) => id !== "edit.toggle_comment")
    .map(({ spec, id }) => ({
      key: codeMirrorKey(spec),
      preventDefault: true,
      run: (view) => {
        latest.current.onCommand(id, view);
        return true;
      },
    }));
  return Prec.highest(keymap.of(bindings));
}
