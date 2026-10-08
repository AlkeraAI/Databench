// Plain text, streams and errors: kernel text with ANSI styling, drawn in the
// app as text nodes.

import type { ErrorInfo } from "../model/types";
import { AnsiText, toText } from "./AnsiText";
import type { OutputRenderer, OutputRendererProps } from "./types";

export function PlainText({ data }: OutputRendererProps) {
  return (
    <pre className="nb-output-text">
      <AnsiText text={toText(data)} />
    </pre>
  );
}

export const textRenderer: OutputRenderer = {
  id: "alkera.text",
  mimes: ["text/plain"],
  rank: 0,
  place: "app",
  Component: PlainText,
};

export function StreamOutput({ name, text }: { name: "stdout" | "stderr"; text: string }) {
  return (
    <pre className={`nb-output-text nb-output-stream nb-output-stream--${name}`} data-stream={name}>
      <AnsiText text={text} />
    </pre>
  );
}

export function ErrorOutput({ error }: { error: ErrorInfo }) {
  const heading = error.evalue ? `${error.ename}: ${error.evalue}` : error.ename;
  return (
    <div className="nb-output-error" role="alert">
      <pre className="nb-output-error-heading">
        <AnsiText text={heading} />
      </pre>
      {error.traceback.length > 0 && (
        <pre className="nb-output-text nb-output-error-traceback">
          <AnsiText text={error.traceback.join("\n")} />
        </pre>
      )}
    </div>
  );
}
