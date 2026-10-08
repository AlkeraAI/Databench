// One cell: its status and dependency gutters, its header, its editor (or a
// Markdown cell's rendering), its outputs, and who ran it.
//
// Presentation only: the notebook editor decides what is active and in which
// mode, and hands in the editor and outputs it built.

import { memo, useState, type ReactNode } from "react";

import { actorLabel, currentActorNames } from "../model/actor";
import type { CellRuntime, DocCell, Presence } from "../model/types";
import { Menu, type MenuItem } from "./Menu";
import { SqlCellSettings } from "./SqlCellSettings";
import { STATUS_LOOK, runFooter } from "./status";

export interface CellLink {
  id: string;
  name: string;
  /** The names that flow along the edge. */
  names?: readonly string[];
}

export interface CellViewProps {
  cell: DocCell;
  index: number;
  runtime: CellRuntime;
  active: boolean;
  editing: boolean;
  narrow: boolean;
  canEdit: boolean;
  canRun: boolean;
  /** Drawn as part of the hovered cell's lineage. */
  highlight: "upstream" | "downstream" | null;
  upstream: readonly CellLink[];
  downstream: readonly CellLink[];
  presence: readonly Presence[];
  /** The host's connection control for a SQL cell; without one the header
   *  shows the connection's name. */
  connection?: ReactNode;
  /** Why the last run this cell was part of did not go through. */
  runProblem?: string | null;
  editor: ReactNode;
  /** A Markdown cell's rendering, shown instead of the editor when it is not
   *  being edited. */
  rendered: ReactNode | null;
  outputs: ReactNode;
  outputCollapsed: boolean;
  /** Actions offered on a graph error (the quick fixes). */
  errorActions: ReactNode | null;
  menu: readonly MenuItem[];
  now: number;
  onActivate(): void;
  onEdit(): void;
  onRun(): void;
  onMenu(id: string): void;
  onHoverLinks(ids: readonly string[] | null): void;
  onJump(id: string): void;
  onToggleCode(): void;
  onToggleOutput(): void;
  /** Change a SQL or Markdown cell's settings (`set_meta`). */
  onSetMeta(meta: Record<string, unknown>): void;
  /** Another cell was dropped on this one, to go before or after it. */
  onDropCell?(cellId: string, where: "before" | "after"): void;
}

/** The drag type a cell travels under, so nothing else dropped here moves one. */
export const CELL_DRAG_TYPE = "application/x-alkera-notebook-cell";

const KIND_LABEL: Record<string, string> = {
  python: "Python",
  sql: "SQL",
  markdown: "Markdown",
  setup: "Setup",
  function: "Function",
  class: "Class",
  unparsable: "Unparsable",
};

function CellViewImpl(props: CellViewProps) {
  const { cell, runtime, active, editing, narrow, canEdit, canRun } = props;
  const look = STATUS_LOOK[runtime.status] ?? STATUS_LOOK.not_run;
  const hidden = cell.config.hide_code === true && !editing;
  const disabled = cell.config.disabled === true;
  const name = cell.name !== "_" ? cell.name : null;
  const footer = runFooter(runtime.last_run, runtime.duration_ms, props.now);
  const others = props.presence;
  const [dropAt, setDropAt] = useState<"before" | "after" | null>(null);
  const dropSide = (event: React.DragEvent<HTMLElement>): "before" | "after" => {
    const box = event.currentTarget.getBoundingClientRect();
    return event.clientY < box.top + box.height / 2 ? "before" : "after";
  };
  return (
    <section
      className={[
        "nb-cell",
        active ? "nb-cell--active" : "",
        editing ? "nb-cell--editing" : "",
        disabled ? "nb-cell--disabled" : "",
        props.highlight ? `nb-cell--${props.highlight}` : "",
        `nb-cell--${runtime.status}`,
        dropAt ? `nb-cell--drop-${dropAt}` : "",
      ].join(" ")}
      onDragOver={(event) => {
        if (!props.onDropCell || !event.dataTransfer.types.includes(CELL_DRAG_TYPE)) return;
        event.preventDefault();
        setDropAt(dropSide(event));
      }}
      onDragLeave={() => setDropAt(null)}
      onDrop={(event) => {
        const moved = event.dataTransfer.getData(CELL_DRAG_TYPE);
        setDropAt(null);
        if (!props.onDropCell || !moved || moved === cell.id) return;
        event.preventDefault();
        props.onDropCell(moved, dropSide(event));
      }}
      data-cell-id={cell.id}
      data-status={runtime.status}
      aria-label={`Cell ${props.index + 1}${name ? `, ${name}` : ""}, ${look.label}`}
      aria-current={active ? "true" : undefined}
      onMouseDown={() => props.onActivate()}
    >
      <div className="nb-cell__gutter">
        {canEdit && props.onDropCell ? (
          <span
            className="nb-cell__handle"
            draggable
            role="img"
            aria-label="Drag to move the cell"
            title="Drag to move the cell"
            onDragStart={(event) => {
              event.dataTransfer.setData(CELL_DRAG_TYPE, cell.id);
              event.dataTransfer.effectAllowed = "move";
            }}
          >
            ⠿
          </span>
        ) : null}
        <span
          className={`nb-status nb-status--${look.tone}${look.live ? " nb-status--live" : ""}`}
          role="img"
          aria-label={look.label}
          title={`${look.label}. ${look.detail}`}
          data-testid="cell-status"
        />
        <DagGutter
          upstream={props.upstream}
          downstream={props.downstream}
          onHover={props.onHoverLinks}
          onJump={props.onJump}
        />
      </div>
      <div className="nb-cell__body">
        <header className="nb-cell__header">
          {/* The name leads, so names line up down the notebook; the cell's
              number and kind follow it, quiet, shown only while the reader
              points at or works in the cell (their hidden space trails the
              name, never sits in front of it). The number is the one the
              agent and the outline use ("Cell 3"). */}
          {name ? <span className="nb-cell__name">{name}</span> : null}
          <span className="nb-cell__kind">
            Cell {props.index + 1} · {KIND_LABEL[cell.kind] ?? "Python"}
          </span>
          {others.length > 0 ? (
            <span className="nb-cell__presence" aria-label={`Here: ${others.map((p) => actorLabel(p)).join(", ")}`}>
              {others.map((p) => (
                <span
                  key={p.id}
                  className={`nb-face nb-face--${p.kind}`}
                  style={{ ["--nb-hue" as string]: String(p.hue) }}
                  title={actorLabel(p)}
                >
                  {initials(p.kind === "agent" ? currentActorNames().agent : actorLabel(p))}
                </span>
              ))}
            </span>
          ) : null}
          <span className="nb-cell__spacer" />
          {canRun && (active || !narrow) ? (
            <button type="button" className="nb-icon-button nb-cell__run" aria-label="Run cell" title="Run cell" onClick={props.onRun}>
              ▶
            </button>
          ) : null}
          {(canEdit || canRun) && (active || !narrow) ? (
            <Menu label="Cell actions" trigger="⋯" items={props.menu} onPick={props.onMenu} />
          ) : null}
        </header>
        {/* A SQL cell's connection and result name take a row of their own
            under the name: a connection picker needs the width. */}
        {cell.kind === "sql" ? (
          <div className="nb-cell__sql-row">
            {props.connection !== undefined ? (
              props.connection
            ) : typeof cell.meta.connection === "string" ? (
              <span className="nb-cell__connection" title="Connection">
                {cell.meta.connection}
              </span>
            ) : null}
            <SqlCellSettings meta={cell.meta} canEdit={canEdit} onSetMeta={props.onSetMeta} />
          </div>
        ) : null}
        {hidden ? (
          <button type="button" className="nb-cell__hidden" onClick={props.onToggleCode}>
            Code hidden. Show code
          </button>
        ) : props.rendered !== null && !editing ? (
          <div
            className="nb-cell__rendered"
            onDoubleClick={canEdit ? props.onEdit : undefined}
            title={canEdit ? "Double-click to edit" : undefined}
          >
            {props.rendered}
          </div>
        ) : (
          props.editor
        )}
        {runtime.outputs.length > 0 ? (
          <div
            className={[
              "nb-cell__outputs",
              runtime.saved ? "nb-cell__outputs--saved" : "",
              runtime.outdated ? "nb-cell__outputs--outdated" : "",
              props.outputCollapsed ? "nb-cell__outputs--collapsed" : "",
              cell.config.expand_output === true ? "nb-cell__outputs--expanded" : "",
            ].join(" ")}
          >
            {runtime.saved || runtime.outdated ? (
              <p className="nb-cell__saved-note">
                {runtime.saved ? "Not run in this kernel" : ""}
                {runtime.saved && runtime.outdated ? ". " : ""}
                {runtime.outdated ? "Outdated: the code or environment changed since" : ""}
              </p>
            ) : null}
            {props.outputCollapsed ? (
              <button type="button" className="nb-cell__expand" onClick={props.onToggleOutput}>
                Output collapsed. Show output
              </button>
            ) : (
              props.outputs
            )}
          </div>
        ) : null}
        {props.runProblem ? (
          <p className="nb-cell__problem" role="status" data-testid="cell-problem">
            {props.runProblem}
          </p>
        ) : runtime.rerun_waits_for !== null ? (
          <p className="nb-cell__note" role="status" data-testid="cell-waiting">
            Not re-run while {runtime.rerun_waits_for} is editing it
          </p>
        ) : runtime.notice !== null ? (
          <p className="nb-cell__note" role="status" data-testid="cell-notice">
            {runtime.notice.message}
          </p>
        ) : null}
        {props.errorActions}
        {footer || runtime.graph_errors.length > 0 ? (
          <footer className="nb-cell__footer">
            {footer ? (
              <>
                <span className="nb-cell__run-when">{footer.when}</span>
                {/* Who ran it waits for hover or focus; it keeps its place in
                    the line (no shift) and in the document (screen readers). */}
                <span className="nb-cell__run-by">{footer.when ? ` · ${footer.by}` : footer.by}</span>
              </>
            ) : null}
          </footer>
        ) : null}
      </div>
    </section>
  );
}

export const CellView = memo(CellViewImpl);

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  return (parts[0]![0]! + (parts.length > 1 ? parts[parts.length - 1]![0]! : "")).toUpperCase();
}

interface DagGutterProps {
  upstream: readonly CellLink[];
  downstream: readonly CellLink[];
  onHover(ids: readonly string[] | null): void;
  onJump(id: string): void;
}

/** Which cells this one reads from and which read from it: hover highlights
 *  them, a click lists them, and a pick jumps there. */
export function DagGutter({ upstream, downstream, onHover, onJump }: DagGutterProps) {
  const [open, setOpen] = useState<"up" | "down" | null>(null);
  if (upstream.length === 0 && downstream.length === 0) return <span className="nb-dag nb-dag--none" />;
  const list = open === "up" ? upstream : open === "down" ? downstream : [];
  return (
    <span className="nb-dag">
      {upstream.length > 0 ? (
        <button
          type="button"
          className="nb-dag__button"
          aria-label={`Reads from ${upstream.length} ${upstream.length === 1 ? "cell" : "cells"}`}
          aria-expanded={open === "up"}
          onMouseEnter={() => onHover(upstream.map((l) => l.id))}
          onMouseLeave={() => onHover(null)}
          onClick={() => setOpen(open === "up" ? null : "up")}
        >
          ↑{upstream.length}
        </button>
      ) : null}
      {downstream.length > 0 ? (
        <button
          type="button"
          className="nb-dag__button"
          aria-label={`Read by ${downstream.length} ${downstream.length === 1 ? "cell" : "cells"}`}
          aria-expanded={open === "down"}
          onMouseEnter={() => onHover(downstream.map((l) => l.id))}
          onMouseLeave={() => onHover(null)}
          onClick={() => setOpen(open === "down" ? null : "down")}
        >
          ↓{downstream.length}
        </button>
      ) : null}
      {open !== null ? (
        <ul className="nb-dag__list" aria-label={open === "up" ? "Reads from" : "Read by"}>
          {list.map((link) => (
            <li key={link.id}>
              <button
                type="button"
                onClick={() => {
                  setOpen(null);
                  onJump(link.id);
                }}
              >
                {link.name}
                {link.names && link.names.length > 0 ? <span className="nb-dag__names"> {link.names.join(", ")}</span> : null}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </span>
  );
}
