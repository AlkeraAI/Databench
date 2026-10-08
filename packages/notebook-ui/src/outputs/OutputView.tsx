// One cell output, and a cell's list of them, drawn through the registry.

import "./outputs.css";

import { Component, useMemo, type ErrorInfo as ReactErrorInfo, type ReactNode } from "react";

import type { CellOutput, MimeBundle } from "../model/types";
import { AnsiText, toText } from "./AnsiText";
import { defaultOutputRegistry, type OutputRegistry } from "./registry";
import { ErrorOutput, StreamOutput } from "./text";
import type { OutputAreaContext, OutputContext } from "./types";

/** How deep layouts may nest bundles inside bundles. */
export const MAX_BUNDLE_DEPTH = 8;

class RendererBoundary extends Component<{ fallback: ReactNode; children: ReactNode; resetKey: unknown }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidUpdate(previous: { resetKey: unknown }): void {
    if (previous.resetKey !== this.props.resetKey && this.state.failed) this.setState({ failed: false });
  }

  componentDidCatch(_error: Error, _info: ReactErrorInfo): void {
    // The fallback says what happened; the cell keeps drawing.
  }

  render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

function PlainFallback({ bundle, note }: { bundle: MimeBundle; note: string }) {
  const plain = bundle["text/plain"];
  return (
    <>
      <p className="nb-output-note">{note}</p>
      {plain !== undefined && plain !== null && (
        <pre className="nb-output-text">
          <AnsiText text={toText(plain)} />
        </pre>
      )}
    </>
  );
}

/** The reference an output too large to travel inline is carried as. */
export const REF_MIME = "application/vnd.alkera.ref+json";

interface StoredRef {
  sha256: string;
  bytes: number;
}

function storedRef(value: unknown): StoredRef | null {
  if (typeof value !== "object" || value === null) return null;
  const ref = (value as Record<string, unknown>)[REF_MIME];
  if (typeof ref !== "object" || ref === null) return null;
  const { sha256, bytes } = ref as Record<string, unknown>;
  if (typeof sha256 !== "string" || !/^[0-9a-f]{64}$/.test(sha256)) return null;
  return { sha256, bytes: typeof bytes === "number" ? bytes : 0 };
}

function megabytes(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** An output stored beside the notebook rather than carried inline: an
 *  image is shown from its stored copy; anything else is named with its size
 *  and a link to open it. */
function StoredOutput({ mime, stored, context }: { mime: string; stored: StoredRef; context: OutputContext }) {
  const url = context.blobUrl?.(stored.sha256);
  if (url === undefined) {
    return <p className="nb-output-note">Output too large to show here ({megabytes(stored.bytes)}).</p>;
  }
  if (mime.startsWith("image/") && mime !== "image/svg+xml") {
    return (
      <div className="nb-output-mime" data-mime={mime} data-renderer="stored">
        <img className="nb-output-image" src={url} alt="Output image" />
      </div>
    );
  }
  return (
    <p className="nb-output-note">
      This output is {megabytes(stored.bytes)}.{" "}
      <a href={url} target="_blank" rel="noopener noreferrer">
        Open it
      </a>
    </p>
  );
}

export interface BundleViewProps {
  bundle: MimeBundle;
  metadata?: Record<string, unknown>;
  context: OutputContext;
  registry?: OutputRegistry;
}

/** Draws one bundle with the richest renderer the registry has for it. */
export function BundleView({ bundle, metadata, context, registry = defaultOutputRegistry }: BundleViewProps) {
  const choice = registry.pick(bundle, context);
  if (!choice) {
    const types = Object.keys(bundle).join(", ") || "nothing";
    return <p className="nb-output-note">No renderer for {types}</p>;
  }
  const { renderer, mime } = choice;
  const stored = storedRef(bundle[mime]);
  if (stored !== null) return <StoredOutput mime={mime} stored={stored} context={context} />;
  if (mime === "text/plain") {
    // Only the plain fallback would show while a richer output is stored:
    // that one is what the person asked to see.
    const richer = Object.entries(bundle).find(([, value]) => storedRef(value) !== null);
    if (richer) return <StoredOutput mime={richer[0]} stored={storedRef(richer[1])!} context={context} />;
  }
  const Renderer = renderer.Component;
  return (
    <RendererBoundary resetKey={bundle} fallback={<PlainFallback bundle={bundle} note="This output could not be drawn." />}>
      <div className="nb-output-mime" data-mime={mime} data-renderer={renderer.id}>
        <Renderer mime={mime} data={bundle[mime]} bundle={bundle} metadata={metadata} context={context} />
      </div>
    </RendererBoundary>
  );
}

/** The full context for one output: its id, and a `renderBundle` that draws
 *  nested bundles through the same registry, bounded in depth. */
export function outputContext(base: OutputAreaContext, outputId: string, registry: OutputRegistry, depth = 0): OutputContext {
  const context: OutputContext = {
    ...base,
    outputId,
    renderBundle(bundle: MimeBundle, key: string): ReactNode {
      if (depth + 1 >= MAX_BUNDLE_DEPTH) {
        return <p className="nb-output-note" key={key}>Nested too deeply to draw.</p>;
      }
      const child = outputContext(base, `${outputId}/${key}`, registry, depth + 1);
      return <BundleView key={key} bundle={bundle} context={child} registry={registry} />;
    },
  };
  return context;
}

export interface OutputViewProps {
  output: CellOutput;
  context: OutputAreaContext;
  registry?: OutputRegistry;
}

/** One output of a cell: a display bundle, a stream or an error. */
export function OutputView({ output, context, registry = defaultOutputRegistry }: OutputViewProps) {
  const full = useMemo(() => outputContext(context, output.output_id, registry), [context, output.output_id, registry]);
  let body: ReactNode;
  switch (output.type) {
    case "stream":
      body = <StreamOutput name={output.name} text={output.text} />;
      break;
    case "error":
      body = <ErrorOutput error={output.error} />;
      break;
    case "display":
      body = <BundleView bundle={output.data} metadata={output.metadata} context={full} registry={registry} />;
      break;
  }
  return (
    <div className={`nb-output nb-output--${output.type}`} data-output-id={output.output_id} data-nb-theme={context.theme}>
      {body}
    </div>
  );
}

export interface OutputAreaProps {
  outputs: readonly CellOutput[];
  context: OutputAreaContext;
  registry?: OutputRegistry;
}

/** A cell's outputs in order. Consecutive stream chunks of the same name are
 *  drawn as one block, the way a terminal shows them. */
export function OutputArea({ outputs, context, registry = defaultOutputRegistry }: OutputAreaProps) {
  const merged = useMemo(() => mergeStreams(outputs), [outputs]);
  if (merged.length === 0) return null;
  return (
    <div className="nb-output-area" data-nb-theme={context.theme}>
      {merged.map((output) => (
        <OutputView key={output.output_id} output={output} context={context} registry={registry} />
      ))}
    </div>
  );
}

/** Joins adjacent stream outputs of the same name into one. */
export function mergeStreams(outputs: readonly CellOutput[]): CellOutput[] {
  const result: CellOutput[] = [];
  for (const output of outputs) {
    const last = result[result.length - 1];
    if (output.type === "stream" && last?.type === "stream" && last.name === output.name) {
      result[result.length - 1] = { ...last, text: last.text + output.text };
    } else {
      result.push(output);
    }
  }
  return result;
}
