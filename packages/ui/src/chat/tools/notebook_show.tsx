// A notebook output the agent put in the chat. The other notebook calls are
// one quiet line each, because the notebook shows what they did; this one is
// the output itself, so the reader sees the result without opening the
// notebook. Each kind draws through the renderer the rest of the product uses
// for it: a table in the grid a SQL result has, a chart through the Alkera
// chart renderer, Markdown as prose, an image read from where the notebook
// holds it once the card is on screen. Everything shown is the notebook's own
// output: carried as data, or read by its hash under the reader's own access.
import { useEffect, useState, type ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";

import { AlkeraChart } from "../../charts";
import { Markdown } from "../../primitives/render";
import { count, isRecord, num, numericColumns, readResult, str } from "./alkeraPayload";
import { ResultGrid, readRows } from "./resultGrid";
import { goToCell } from "./notebookCell";
import { EmptyLine, PathBand } from "./shared";
import type { CardHead, StepEnvironment, StoredOutputRead } from "./step";
import "./shared.css";
import s from "./notebook_show.module.css";

type Kind = "table" | "chart" | "image" | "markdown" | "text" | "error";

const KINDS: readonly Kind[] = ["table", "chart", "image", "markdown", "text", "error"];

/** What a head says the call showed, after the cell's name. */
const KIND_WORD: Record<Kind, string> = {
  table: "table",
  chart: "chart",
  image: "image",
  markdown: "text",
  text: "text",
  error: "error",
};

interface Shown {
  kind: Kind | null;
  /** The notebook as the tool answered it: its path in the workspace, not the
   *  machine's spelling the agent may have typed. */
  path: string;
  cellId: string;
  cellName: string;
  note: string;
  columns: string[];
  rows: ReturnType<typeof readRows>;
  shownRows: number;
  totalRows: number;
  spec: unknown;
  /** A chart too large for the reply: the hash of the file its spec is in. */
  chartSha256: string;
  sha256: string;
  text: string;
  errorName: string;
  errorText: string;
  traceback: string;
}

/** The value a notebook produced, out of the wrapper that marks it as data. */
function content(value: unknown): unknown {
  return isRecord(value) && value.untrusted === true ? value.content : undefined;
}

function derive(part: ToolConversationPart): Shown {
  const result = readResult(part.output) ?? {};
  const kind = KINDS.find((known) => known === result.kind) ?? null;
  const table: Record<string, unknown> = isRecord(result.table) ? result.table : {};
  const image: Record<string, unknown> = isRecord(result.image) ? result.image : {};
  const error: Record<string, unknown> = isRecord(result.cell_error) ? result.cell_error : {};
  const rows = readRows(content(table.rows));
  const shown = kind === "markdown" ? content(result.markdown) : content(result.text);
  return {
    kind,
    path: str(result.path) || str(part.input?.path),
    cellId: str(result.cell_id),
    cellName: str(result.cell_name) || str(part.input?.cell),
    note: str(result.note),
    columns: Array.isArray(table.columns) ? table.columns.map(String) : [],
    rows,
    shownRows: num(table.shown_rows) ?? rows.length,
    totalRows: num(table.total_rows) ?? rows.length,
    spec: content(result.chart_spec),
    chartSha256: isRecord(result.chart_ref) ? str(result.chart_ref.sha256) : "",
    sha256: str(image.sha256),
    text: typeof shown === "string" ? shown : "",
    errorName: str(error.ename),
    errorText: str(content(error.evalue)),
    traceback: str(content(error.traceback)),
  };
}

/** A stored output's read, as far as it has got. `waiting` is a card not yet
 *  on screen, which asks for nothing; `unwired` is a shell with nowhere to
 *  read stored outputs from. */
type Loaded<T> = { kind: "waiting" } | { kind: "loading" } | { kind: "failed" } | { kind: "unwired" } | StoredOutputRead<T>;

/** Whether the element has come near the screen once. Without an observer
 *  (a renderer with no layout) it counts as seen. */
function useSeen<T extends Element>(): [(element: T | null) => void, boolean] {
  // A callback ref: the frame may mount after the card does (a reader that
  // arrives late), and is watched from whenever it does.
  const [element, setElement] = useState<T | null>(null);
  const [seen, setSeen] = useState(() => typeof IntersectionObserver === "undefined");
  useEffect(() => {
    if (seen || !element) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        setSeen(true);
        observer.disconnect();
      },
      { rootMargin: "200px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [element, seen]);
  return [setElement, seen];
}

/** Reads a stored output once its card is on screen, and not before. */
function useStoredOutput<T>(
  read: ((path: string, sha256: string) => Promise<StoredOutputRead<T>>) | undefined,
  path: string,
  sha256: string,
  seen: boolean,
): Loaded<T> {
  const [state, setState] = useState<Loaded<T>>(read ? { kind: "waiting" } : { kind: "unwired" });
  useEffect(() => {
    if (!read) {
      setState({ kind: "unwired" });
      return;
    }
    if (!seen) return;
    let current = true;
    setState({ kind: "loading" });
    read(path, sha256).then(
      (found) => current && setState(found),
      () => current && setState({ kind: "failed" }),
    );
    return () => {
      current = false;
    };
  }, [read, path, sha256, seen]);
  return state;
}

/** What a card says in place of an output it cannot show, by why. */
function absentNote(state: Loaded<unknown>, noun: "image" | "chart"): string | null {
  switch (state.kind) {
    case "unwired":
      return `Open the notebook to see this ${noun}.`;
    case "refused":
      return "You don't have access to this notebook.";
    case "gone":
      return `This ${noun} is no longer in the notebook. The cell ran again or was deleted.`;
    case "failed":
      return `The ${noun} could not be loaded.`;
    default:
      return null;
  }
}

/** A stored output's frame: the note when there is nothing to show, the
 *  loading placeholder (at the output's own height, so the transcript under
 *  it does not jump) until there is. */
function StoredFrame({
  state,
  noun,
  label,
  frame,
  children,
}: {
  state: Loaded<unknown>;
  noun: "image" | "chart";
  label: string;
  frame: (element: HTMLDivElement | null) => void;
  children?: ReactElement | null;
}): ReactElement {
  const note = absentNote(state, noun);
  if (note !== null) return <EmptyLine>{note}</EmptyLine>;
  return (
    <div ref={frame} className={s.stored}>
      {children ?? (
        <div className={s.loading} role="status" aria-label={label}>
          {`Loading the ${noun}.`}
        </div>
      )}
    </div>
  );
}

/** An image a cell shows, read by its hash once the card is on screen and
 *  drawn no taller than the card allows ("Go to cell" shows it whole). */
function NotebookImage({
  path,
  sha256,
  label,
  read,
}: {
  path: string;
  sha256: string;
  label: string;
  read: StepEnvironment["notebookImage"];
}): ReactElement {
  const [frame, seen] = useSeen<HTMLDivElement>();
  const state = useStoredOutput(read, path, sha256, seen);
  const [url, setUrl] = useState<string | null>(null);
  const [broken, setBroken] = useState(false);
  const bytes = state.kind === "ready" ? state.value : null;
  useEffect(() => {
    if (bytes === null) return;
    const made = URL.createObjectURL(bytes);
    setUrl(made);
    return () => {
      URL.revokeObjectURL(made);
      setUrl(null);
    };
  }, [bytes]);
  const shown: Loaded<unknown> = broken ? { kind: "failed" } : state;
  return (
    <StoredFrame state={shown} noun="image" label={label} frame={frame}>
      {url ? (
        // Bytes this browser cannot draw say so, never a broken-image mark.
        <img className={s.image} src={url} alt={label} onError={() => setBroken(true)} />
      ) : null}
    </StoredFrame>
  );
}

/** A chart whose spec the notebook stores beside itself, read once the card
 *  is on screen and drawn at any size. */
function StoredChart({
  path,
  sha256,
  label,
  read,
}: {
  path: string;
  sha256: string;
  label: string;
  read: StepEnvironment["notebookChartSpec"];
}): ReactElement {
  const [frame, seen] = useSeen<HTMLDivElement>();
  const state = useStoredOutput(read, path, sha256, seen);
  const shown: Loaded<unknown> = state.kind === "ready" && !isRecord(state.value) ? { kind: "failed" } : state;
  return (
    <StoredFrame state={shown} noun="chart" label={label} frame={frame}>
      {shown.kind === "ready" ? (
        <div className={s.chart}>
          <AlkeraChart spec={shown.value} ariaLabel={label} />
        </div>
      ) : null}
    </StoredFrame>
  );
}

function Output({ shown, env }: { shown: Shown; env?: StepEnvironment }): ReactElement | null {
  switch (shown.kind) {
    case "table":
      return shown.columns.length > 0 ? (
        <ResultGrid
          columns={shown.columns}
          rows={shown.rows}
          numeric={numericColumns(shown.columns, shown.rows)}
          label={`${shown.cellName} table`}
        />
      ) : (
        <EmptyLine>The table has no columns.</EmptyLine>
      );
    case "chart":
      if (shown.spec !== undefined) {
        return (
          <div className={s.chart}>
            <AlkeraChart spec={shown.spec} ariaLabel={`${shown.cellName} chart`} />
          </div>
        );
      }
      return shown.chartSha256 ? (
        <StoredChart
          path={shown.path}
          sha256={shown.chartSha256}
          label={`${shown.cellName} chart`}
          read={env?.notebookChartSpec}
        />
      ) : null;
    case "image":
      return (
        <NotebookImage
          path={shown.path}
          sha256={shown.sha256}
          label={`${shown.cellName} image`}
          read={env?.notebookImage}
        />
      );
    case "markdown":
      return (
        <div className={s.prose}>
          <Markdown content={shown.text} />
        </div>
      );
    case "text":
      return <pre className={`chat-tool-mono ${s.text}`}>{shown.text}</pre>;
    case "error":
      return (
        <div className={s.error} role="group" aria-label={`${shown.cellName} error`}>
          <p className="chat-tool-mono">
            {shown.errorName}
            {shown.errorText ? `: ${shown.errorText}` : ""}
          </p>
          {shown.traceback ? <pre className={`chat-tool-mono ${s.text}`}>{shown.traceback}</pre> : null}
        </div>
      );
    default:
      return null;
  }
}

function Body({ part, env }: { part: ToolConversationPart; env?: StepEnvironment }): ReactElement {
  const shown = derive(part);
  const open = env?.onOpenPath;
  return (
    <div data-tool="notebook_show">
      {shown.path ? (
        <PathBand
          path={shown.path}
          relativeTo={env?.workspaceRoot}
          onOpen={open ? () => open(shown.path) : undefined}
          action={part.state === "error" ? undefined : goToCell(env, shown.path, shown.cellId, shown.cellName)}
        />
      ) : null}
      {/* A call that failed showed nothing: its reason is the group's error line. */}
      {part.state === "error" ? null : <Output shown={shown} env={env} />}
      {shown.note ? <p className="chat-tool-note">{shown.note}</p> : null}
    </div>
  );
}

function summary(shown: Shown): string {
  if (shown.kind === null) return "";
  if (shown.kind !== "table") return KIND_WORD[shown.kind];
  return shown.shownRows < shown.totalRows
    ? `${shown.shownRows} of ${count(shown.totalRows, "row", "rows")}`
    : count(shown.totalRows, "row", "rows");
}

/** The step this tool contributes: the cell it showed, and the output under it. */
export const head: CardHead = (part, env) => {
  const shown = derive(part);
  const text = part.state === "error" ? "Failed" : summary(shown);
  return {
    object: shown.cellName || "a cell",
    data: text ? { kind: "count", text } : undefined,
    body: <Body part={part} env={env} />,
  };
};
