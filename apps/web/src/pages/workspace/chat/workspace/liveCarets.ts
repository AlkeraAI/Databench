// Everybody else's carets in a CodeMirror editor (a live file's, or a notebook
// cell's): a bar in their colour at where they stand, their name on a flag
// (shown again each time they move), and their selection tinted. The binding
// says where each caret is; this draws it.
//
// The bar and the tint are decorations inside the text. The name flags are
// not: the editor's scroller clips whatever it holds, and a flag on the first
// or last line of a short cell, or near its right edge, reaches past the
// editor's box. The flags are drawn in a layer of their own beside the
// scroller, placed from where each caret's text stands and placed again
// whenever the editor scrolls or its layout changes.

import { StateEffect, StateField, type Text } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, WidgetType, type DecorationSet, type ViewUpdate } from "@codemirror/view";

import type { CaretLayer, EditorCaret } from "@/api/realtime/crdt/codeMirrorBinding";

import "./liveCarets.css";

const setCarets = StateEffect.define<readonly EditorCaret[]>();

/** The carets drawn, with each one's position kept up with the text. */
interface Drawn {
  carets: readonly EditorCaret[];
  decorations: DecorationSet;
}

class CaretBar extends WidgetType {
  constructor(readonly hue: number) {
    super();
  }

  override eq(other: CaretBar): boolean {
    return other.hue === this.hue;
  }

  toDOM(): HTMLElement {
    const caret = document.createElement("span");
    caret.className = "alk-cm-caret";
    caret.style.setProperty("--live-file-caret-hue", String(this.hue));
    caret.setAttribute("aria-hidden", "true");
    return caret;
  }

  override ignoreEvent(): boolean {
    return true;
  }
}

function caretDecorations(carets: readonly EditorCaret[]): DecorationSet {
  const ranges = [];
  for (const caret of carets) {
    if (caret.head !== caret.anchor) {
      ranges.push(
        Decoration.mark({
          class: "alk-cm-caret-range",
          attributes: { style: `--live-file-caret-hue: ${caret.hue}` },
        }).range(Math.min(caret.head, caret.anchor), Math.max(caret.head, caret.anchor)),
      );
    }
    ranges.push(Decoration.widget({ widget: new CaretBar(caret.hue), side: 1 }).range(caret.head));
  }
  return Decoration.set(ranges, true);
}

function clamp(carets: readonly EditorCaret[], doc: Text): EditorCaret[] {
  return carets.map((caret) => ({
    ...caret,
    head: Math.min(caret.head, doc.length),
    anchor: Math.min(caret.anchor, doc.length),
  }));
}

const caretField = StateField.define<Drawn>({
  create: () => ({ carets: [], decorations: Decoration.none }),
  update(drawn, tr) {
    let next = drawn;
    if (tr.docChanged && drawn.carets.length > 0) {
      next = {
        carets: drawn.carets.map((caret) => ({
          ...caret,
          head: tr.changes.mapPos(caret.head, 1),
          anchor: tr.changes.mapPos(caret.anchor, 1),
        })),
        decorations: drawn.decorations.map(tr.changes),
      };
    }
    for (const effect of tr.effects) {
      if (!effect.is(setCarets)) continue;
      const carets = clamp(effect.value, tr.state.doc);
      next = { carets, decorations: caretDecorations(carets) };
    }
    return next;
  },
  provide: (field) => EditorView.decorations.from(field, (drawn) => drawn.decorations),
});

interface Flag {
  element: HTMLElement;
  name: string;
  hue: number;
  moves: number;
}

/** Where each flag goes, in the layer's coordinates; null when its caret is
 *  out of the scroller's sight. */
type Placements = Map<string, { left: number; top: number } | null>;

/** The flags' layer: a child of the editor's outer element, a sibling of the
 *  scroller, so nothing the scroller clips can cut a name off. */
class CaretFlags {
  private readonly layer: HTMLElement;
  private readonly flags = new Map<string, Flag>();
  private readonly onScroll = (): void => this.place();

  constructor(private readonly view: EditorView) {
    this.layer = document.createElement("div");
    this.layer.className = "alk-cm-caret-flags";
    this.layer.setAttribute("aria-hidden", "true");
    view.dom.appendChild(this.layer);
    view.scrollDOM.addEventListener("scroll", this.onScroll, { passive: true });
    this.sync(view.state.field(caretField).carets, view.state.doc);
  }

  update(update: ViewUpdate): void {
    const carets = update.state.field(caretField).carets;
    if (carets !== update.startState.field(caretField).carets) this.sync(carets, update.state.doc);
    else if (this.flags.size > 0 && (update.viewportChanged || update.geometryChanged)) this.place();
  }

  destroy(): void {
    this.view.scrollDOM.removeEventListener("scroll", this.onScroll);
    this.layer.remove();
  }

  /** One flag per caret. A caret that moved gets a new one, so its name shows
   *  again and fades. */
  private sync(carets: readonly EditorCaret[], doc: Text): void {
    const seen = new Set<string>();
    for (const caret of carets) {
      seen.add(caret.id);
      let flag = this.flags.get(caret.id);
      if (flag === undefined || flag.moves !== caret.moves || flag.name !== caret.name || flag.hue !== caret.hue) {
        flag?.element.remove();
        flag = { element: newFlag(caret), name: caret.name, hue: caret.hue, moves: caret.moves };
        this.flags.set(caret.id, flag);
        this.layer.appendChild(flag.element);
      }
      // On the first line the name hangs below the caret: above it, it would
      // sit over whatever is above the editor.
      flag.element.classList.toggle("alk-cm-caret__name--below", doc.lineAt(caret.head).number === 1);
    }
    for (const [id, flag] of this.flags) {
      if (seen.has(id)) continue;
      flag.element.remove();
      this.flags.delete(id);
    }
    if (this.flags.size > 0) this.place();
  }

  private place(): void {
    this.view.requestMeasure<Placements>({
      key: this,
      read: (view) => this.measure(view),
      write: (placements) => this.write(placements),
    });
  }

  private measure(view: EditorView): Placements {
    const out: Placements = new Map();
    const outer = view.dom.getBoundingClientRect();
    const sight = view.scrollDOM.getBoundingClientRect();
    const scaleX = view.scaleX || 1;
    const scaleY = view.scaleY || 1;
    for (const caret of view.state.field(caretField).carets) {
      const at = view.coordsAtPos(caret.head, 1);
      const inSight =
        at !== null && at.bottom > sight.top && at.top < sight.bottom && at.left >= sight.left - 1 && at.left <= sight.right + 1;
      if (at === null || !inSight) {
        out.set(caret.id, null);
        continue;
      }
      const below = this.flags.get(caret.id)?.element.classList.contains("alk-cm-caret__name--below") === true;
      out.set(caret.id, {
        left: (at.left - outer.left) / scaleX - view.dom.clientLeft,
        top: ((below ? at.bottom : at.top) - outer.top) / scaleY - view.dom.clientTop,
      });
    }
    return out;
  }

  private write(placements: Placements): void {
    for (const [id, flag] of this.flags) {
      const where = placements.get(id) ?? null;
      if (where === null) {
        flag.element.style.visibility = "hidden";
        continue;
      }
      flag.element.style.left = `${where.left}px`;
      flag.element.style.top = `${where.top}px`;
      flag.element.style.visibility = "";
    }
  }
}

function newFlag(caret: EditorCaret): HTMLElement {
  const label = document.createElement("span");
  label.className = "alk-cm-caret__name";
  label.style.setProperty("--live-file-caret-hue", String(caret.hue));
  // Unplaced until measured: never a flash at the layer's corner.
  label.style.visibility = "hidden";
  label.textContent = caret.name;
  return label;
}

const caretFlags = ViewPlugin.fromClass(CaretFlags);

/** The caret layer every live editor draws with. */
export const liveCarets: CaretLayer = {
  extension: [caretField, caretFlags],
  draw: (carets) => setCarets.of(carets),
};
