import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent, type MouseEvent } from "react";

import { useScrollShadow } from "../../../hooks/useScrollShadow";
import { cx } from "../../cx";
import type { TabStripItem, TabStripProps } from "./types";

// TabStrip — the editor row of open files. Tablist semantics with automatic
// activation (WAI-ARIA tabs): Left/Right/Home/End move focus AND select, and a
// roving tabindex keeps the whole strip to one tab stop, so a reader arrows
// across it instead of tabbing through every open file.
//
// The strip holds none of the state it draws. It reports an activation and a
// close; what it owns is the part a host cannot see — which tab the reader is
// left standing on after one disappears (the right neighbour, then the left,
// then the pinned lead), so focus never falls back to the document.
//
// A tab is a div rather than a button because the close control nests inside
// it, and the close control is out of the tab order on purpose: Delete and
// Backspace close the focused tab, which is the keyboard route. The trailing
// controls sit beside the tablist rather than inside it — a tablist owns tabs
// and nothing else.
//
// A preview tab is drawn in italics and kept by a double click. Dragging is the
// host's to allow: a drag carries the host's own payload under the host's own
// type, and a drop reports where among the drawn tabs it landed (before the tab
// under the pointer's left half, after it on the right half, at the end past the
// last one), so a host can move a tab along its strip or take one from another.

/** What a marker dot says out loud. */
const MARKER_LABEL = { updated: "updated", gone: "no longer available" } as const;

export function TabStrip({ label, tabs, activeId, onActivate, onClose, onPin, drag, trailing }: TabStripProps) {
  const list = useScrollShadow<HTMLDivElement>();
  const nodes = useRef(new Map<string, HTMLDivElement>());
  /** The tab to stand on once the list re-renders without the one just closed. */
  const pendingFocus = useRef<string | null>(null);

  const activeIndex = tabs.findIndex((tab) => tab.id === activeId);
  /** Where a drag hovering over the strip would land, for the drop marker. */
  const [dropAt, setDropAt] = useState<number | null>(null);

  const carries = (event: DragEvent<HTMLElement>): boolean =>
    drag !== undefined && Array.from(event.dataTransfer?.types ?? []).includes(drag.type);

  /** The drop index for a pointer over the tab at `index`: before it on its left
   *  half, after it on its right half. */
  const indexOver = (event: DragEvent<HTMLElement>, index: number): number => {
    const rect = event.currentTarget.getBoundingClientRect();
    return rect.width > 0 && event.clientX > rect.left + rect.width / 2 ? index + 1 : index;
  };

  const over = (event: DragEvent<HTMLElement>, at: number): void => {
    if (!carries(event)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "move";
    if (dropAt !== at) setDropAt(at);
  };

  const dropped = (event: DragEvent<HTMLElement>, at: number): void => {
    if (!drag || !carries(event)) return;
    event.preventDefault();
    event.stopPropagation();
    setDropAt(null);
    const payload = event.dataTransfer.getData(drag.type);
    if (payload) drag.onDrop(payload, at);
  };

  useEffect(() => {
    if (activeId === null) return;
    nodes.current.get(activeId)?.scrollIntoView({ inline: "nearest", block: "nearest" });
  }, [activeId]);

  useEffect(() => {
    const id = pendingFocus.current;
    if (id === null) return;
    pendingFocus.current = null;
    nodes.current.get(id)?.focus();
  });

  /** Where focus goes when the tab at `index` leaves: right, then left, then the pinned lead. */
  const heir = (index: number): string | null => {
    const neighbour = tabs[index + 1] ?? tabs[index - 1];
    if (neighbour !== undefined) return neighbour.id;
    return tabs.find((tab) => tab.pinned)?.id ?? null;
  };

  const close = (index: number): void => {
    const tab = tabs[index];
    if (tab === undefined || tab.pinned === true) return;
    pendingFocus.current = heir(index);
    onClose(tab.id);
  };

  const focusAndSelect = (index: number): void => {
    const tab = tabs[index];
    if (tab === undefined) return;
    onActivate(tab.id);
    nodes.current.get(tab.id)?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>, index: number): void => {
    if (event.key === "Delete" || event.key === "Backspace") {
      if (tabs[index]?.pinned === true) return;
      event.preventDefault();
      close(index);
      return;
    }
    const last = tabs.length - 1;
    const next =
      event.key === "ArrowRight" ? (index >= last ? 0 : index + 1)
      : event.key === "ArrowLeft" ? (index <= 0 ? last : index - 1)
      : event.key === "Home" ? 0
      : event.key === "End" ? last
      : null;
    if (next === null || tabs[next] === undefined) return;
    event.preventDefault();
    focusAndSelect(next);
  };

  const onAuxClick = (event: MouseEvent<HTMLDivElement>, index: number): void => {
    // The middle button closes the tab under the pointer, the way an editor does.
    if (event.button !== 1) return;
    event.preventDefault();
    close(index);
  };

  return (
    <div className="alk-tabstrip">
      <div
        ref={list}
        className="alk-tabstrip__list"
        role="tablist"
        aria-label={label}
        aria-orientation="horizontal"
        data-drop-end={dropAt === tabs.length || undefined}
        onDragOver={(event) => over(event, tabs.length)}
        onDragLeave={(event) => {
          // Leaving for a tab inside the strip is not leaving the strip.
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDropAt(null);
        }}
        onDrop={(event) => dropped(event, tabs.length)}
      >
        {tabs.map((tab: TabStripItem, index) => {
          const selected = tab.id === activeId;
          const marker = tab.marker ?? null;
          return (
            <div
              key={tab.id}
              ref={(el) => {
                if (el === null) nodes.current.delete(tab.id);
                else nodes.current.set(tab.id, el);
              }}
              id={`tab-${tab.id}`}
              className={cx(
                "alk-tabstrip__tab",
                tab.pinned === true && "alk-tabstrip__tab--pinned",
                tab.transient === true && "alk-tabstrip__tab--transient",
              )}
              role="tab"
              aria-selected={selected}
              aria-controls={`panel-${tab.id}`}
              tabIndex={selected || (activeIndex < 0 && index === 0) ? 0 : -1}
              data-on={selected || undefined}
              data-drop={dropAt === index ? "before" : undefined}
              draggable={drag !== undefined || undefined}
              onDragStart={(event) => {
                if (!drag) return;
                event.dataTransfer.setData(drag.type, drag.payload(tab.id));
                event.dataTransfer.effectAllowed = "move";
                drag.onDragStart?.(tab.id);
              }}
              onDragEnd={() => {
                setDropAt(null);
                drag?.onDragEnd?.();
              }}
              onDragOver={(event) => over(event, indexOver(event, index))}
              onDrop={(event) => dropped(event, indexOver(event, index))}
              onClick={() => onActivate(tab.id)}
              onDoubleClick={() => onPin?.(tab.id)}
              onKeyDown={(event) => onKeyDown(event, index)}
              onAuxClick={(event) => onAuxClick(event, index)}
              onMouseDown={(event) => {
                // Without this the middle button starts the browser's autoscroll.
                if (event.button === 1) event.preventDefault();
              }}
            >
              {tab.icon != null ? (
                <span className="alk-tabstrip__icon" aria-hidden>
                  {tab.icon}
                </span>
              ) : null}
              <span className="alk-tabstrip__label" title={tab.title ?? tab.label}>
                {tab.label}
              </span>
              {marker !== null ? (
                <span
                  className="alk-tabstrip__marker"
                  data-marker={marker}
                  role="img"
                  aria-label={MARKER_LABEL[marker]}
                />
              ) : null}
              {tab.pinned === true ? null : (
                <button
                  type="button"
                  className="alk-tabstrip__close"
                  tabIndex={-1}
                  aria-label={`Close ${tab.label}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    close(index);
                  }}
                >
                  <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden focusable="false">
                    <path
                      d="M3 3l6 6M9 3l-6 6"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                    />
                  </svg>
                </button>
              )}
            </div>
          );
        })}
      </div>
      {trailing != null ? <div className="alk-tabstrip__trailing">{trailing}</div> : null}
    </div>
  );
}
