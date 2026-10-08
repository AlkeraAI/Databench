// A notebook to read, not to work in: the same cells and outputs the editor
// draws, with nothing that changes the document or reaches a kernel. No
// toolbar, no run buttons, no cell menus, no panels; each cell's code sits in
// a read-only editor so it is highlighted as the editor highlights it.
//
// A host that can show a notebook only as stored (a file preview, a shared
// link) hands it the cells and the outputs it has; nothing here fetches.

import { useEffect, useMemo, useState, type ReactNode } from "react";

import { EMPTY_RUNTIME, type CellRuntime, type DocCell } from "../model/types";
import { OutputArea, OutputView } from "../outputs/OutputView";
import { CellEditor } from "./CellEditor";
import { CellView } from "./CellView";
import { MemoryNotebook } from "./memoryHost";
import type { NotebookOutputSettings } from "./ports";

export interface NotebookReaderProps {
  cells: readonly DocCell[];
  /** What each cell shows under it: its outputs and who ran it last. */
  runtime: Readonly<Record<string, CellRuntime | undefined>>;
  output: NotebookOutputSettings;
  /** Wall clock for "2 min ago"; injectable for tests. */
  now?: () => number;
}

const NOTHING = (): void => undefined;

export function NotebookReader({
  cells,
  runtime,
  output,
  now,
}: NotebookReaderProps) {
  // The editor binds its text through a document port; a read-only one
  // holding the cells as given is all it needs.
  const doc = useMemo(() => new MemoryNotebook(cells, false), [cells]);
  const bindings = useMemo(
    () => new Map(cells.map((cell) => [cell.id, doc.bind(cell.id)])),
    [cells, doc],
  );
  useEffect(
    () => () => bindings.forEach((binding) => binding.dispose()),
    [bindings],
  );
  /** Cells whose hidden code the reader asked to see. */
  const [shown, setShown] = useState<ReadonlySet<string>>(new Set());
  const at = (now ?? Date.now)();

  const context = (cellId: string) => ({
    theme: output.theme,
    readonly: true,
    cellId,
    ...(output.blobUrl ? { blobUrl: output.blobUrl } : {}),
  });

  const rendered = (cell: DocCell): ReactNode =>
    cell.kind !== "markdown" || cell.source.trim() === "" ? null : (
      <OutputView
        output={{
          output_id: `${cell.id}/md`,
          type: "display",
          data: { "text/markdown": cell.source },
        }}
        context={context(cell.id)}
      />
    );

  return (
    <div
      className="nb-root nb-notebook nb-notebook--reader"
      data-nb-theme={output.theme}
      data-testid="notebook-reader"
    >
      <div className="nb-notebook__main">
        <div className="nb-cells" role="list" aria-label="Cells">
          {cells.map((raw, index) => {
            const cell = shown.has(raw.id)
              ? { ...raw, config: { ...raw.config, hide_code: false } }
              : raw;
            const cellRuntime = runtime[cell.id] ?? EMPTY_RUNTIME;
            const binding = bindings.get(cell.id);
            return (
              <div role="listitem" key={cell.id}>
                <CellView
                  cell={cell}
                  index={index}
                  runtime={cellRuntime}
                  active={false}
                  editing={false}
                  narrow={false}
                  canEdit={false}
                  canRun={false}
                  highlight={null}
                  upstream={[]}
                  downstream={[]}
                  presence={[]}
                  editor={
                    binding ? (
                      <CellEditor
                        binding={binding}
                        kind={cell.kind}
                        readOnly
                        keys={[]}
                        onCommand={NOTHING}
                        onFocus={NOTHING}
                      />
                    ) : null
                  }
                  rendered={rendered(cell)}
                  outputs={
                    <OutputArea
                      outputs={cellRuntime.outputs}
                      context={context(cell.id)}
                    />
                  }
                  outputCollapsed={false}
                  errorActions={null}
                  menu={[]}
                  now={at}
                  onActivate={NOTHING}
                  onEdit={NOTHING}
                  onRun={NOTHING}
                  onMenu={NOTHING}
                  onHoverLinks={NOTHING}
                  onJump={NOTHING}
                  onToggleCode={() =>
                    setShown((prev) => new Set(prev).add(raw.id))
                  }
                  onToggleOutput={NOTHING}
                  onSetMeta={NOTHING}
                />
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
