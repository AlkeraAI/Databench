// A notebook output drawn in the content-origin frame.
//
// The frame is a fresh document per output (and per recycle) on the content
// origin, sandboxed to scripts only. This component owns its element and its
// size; everything said across the boundary goes through the `FrameHost`.

import { useEffect, useRef, useState, type ReactElement } from "react";

import type { FrameServices, OutputRenderer, OutputRendererProps, OutputTheme } from "../outputs/types";
import { FrameHost } from "./host";
import { FRAME_MODULE_BY_MIME, WIDGET_VIEW_MIME } from "./protocol";
import "./frame.css";

/** How long a frame may take to answer before it is called unreachable. */
export const FRAME_LOAD_TIMEOUT_MS = 15_000;

const UNREACHABLE = "The content server didn’t answer, so this output couldn’t be loaded.";
const NAVIGATED = "This output tried to leave its frame, so it was stopped.";
const NO_FRAME = "This output can’t be shown here.";

type Status = "loading" | "shown" | "unreachable" | "navigated";

interface View {
  host: FrameHost;
  nonce: string;
  src: string;
}

interface InnerProps {
  services: FrameServices;
  mime: string;
  data: unknown;
  outputId: string;
  theme: OutputTheme;
  readonly: boolean;
}

function FramedOutputFrame({ services, mime, data, outputId, theme, readonly }: InnerProps): ReactElement {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const themeRef = useRef(theme);
  themeRef.current = theme;
  const [view, setView] = useState<View | null>(null);
  const [status, setStatus] = useState<Status>("loading");
  const [height, setHeight] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  // One host per output. Built in the effect (not during render) so a
  // remount, or React's development double-run, never reuses a disposed one.
  useEffect(() => {
    const host = new FrameHost({
      services,
      outputId,
      mime,
      data,
      theme: themeRef.current,
      readonly,
      events: {
        onReady: () => setStatus("shown"),
        onSize: (next) => setHeight(next),
        onError: (message) => setError(message),
        onNavigated: () => setStatus("navigated"),
        onRecycle: (nonce, src) => {
          setView({ host, nonce, src });
          setStatus("loading");
          setError(null);
        },
      },
    });
    host.attach(() => iframeRef.current?.contentWindow ?? null);
    const onMessage = (event: MessageEvent): void => {
      host.handleMessage(event);
    };
    window.addEventListener("message", onMessage);
    setView({ host, nonce: host.nonce, src: host.src });
    setStatus("loading");
    setError(null);
    return () => {
      window.removeEventListener("message", onMessage);
      host.dispose();
    };
  }, [services, outputId, mime, data, readonly]);

  useEffect(() => {
    view?.host.setTheme(theme);
  }, [view, theme]);

  // One watchdog per attempt: a frame that never answers is reported, with a
  // way to try again, instead of leaving a blank box.
  useEffect(() => {
    if (status !== "loading" || !view) return;
    const giveUp = setTimeout(() => setStatus("unreachable"), FRAME_LOAD_TIMEOUT_MS);
    return () => clearTimeout(giveUp);
  }, [status, view]);

  if (!view) return <div className="nb-frame" />;

  if (status === "unreachable" || status === "navigated") {
    return (
      <div className="nb-frame">
        <div className="nb-frame__notice" role="status">
          <span>{status === "unreachable" ? UNREACHABLE : NAVIGATED}</span>
          <button type="button" className="nb-frame__button" onClick={() => view.host.recycle()}>
            {status === "unreachable" ? "Try again" : "Reload output"}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="nb-frame">
      <iframe
        // A recycle is a new document, not a new fragment: only a new element
        // reloads, since a change of `#n=` alone would not.
        key={view.nonce}
        ref={iframeRef}
        className="nb-frame__frame"
        src={view.src}
        title="Output"
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        allow=""
        style={height === null ? undefined : { height: `${height}px` }}
        onLoad={() => view.host.handleLoad()}
      />
      {error ? (
        <p className="nb-frame__error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** An output drawn in the content frame. Without frame services it says so
 *  rather than drawing the output in the app. The frame is rebuilt when the
 *  services, the output or its read-only state change, so a host passes the
 *  same `context.frame` object from render to render. */
export function FramedOutput({ mime, data, context }: OutputRendererProps): ReactElement {
  if (!context.frame) {
    return (
      <div className="nb-frame">
        <div className="nb-frame__notice" role="status">
          {NO_FRAME}
        </div>
      </div>
    );
  }
  return (
    <FramedOutputFrame
      services={context.frame}
      mime={mime}
      data={data}
      outputId={context.outputId}
      theme={context.theme}
      readonly={context.readonly}
    />
  );
}

const framed = (id: string, mimes: readonly string[], rank: number): OutputRenderer => ({
  id,
  mimes,
  rank,
  place: "frame",
  available: (context) => context.frame !== undefined,
  Component: FramedOutput,
});

/** The renderers that draw in the content frame, each available only where
 *  the host can serve one. */
export const frameRenderers: OutputRenderer[] = [
  framed("frame-widget", [WIDGET_VIEW_MIME], 90),
  framed("frame-plotly", ["application/vnd.plotly.v1+json"], 80),
  framed("frame-vega-lite", ["application/vnd.vegalite.v5+json", "application/vnd.vegalite.v6+json"], 80),
  framed("frame-html", ["text/html"], 60),
  framed("frame-svg", ["image/svg+xml"], 60),
];

/** Every MIME type the frame draws. */
export const FRAME_MIMES: readonly string[] = Object.keys(FRAME_MODULE_BY_MIME);
