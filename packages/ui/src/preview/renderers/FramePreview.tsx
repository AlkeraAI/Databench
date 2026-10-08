import { useCallback, useEffect, useState, type ReactElement } from "react";

import { Button } from "../../primitives/controls/Button";
import type { PreviewProps } from "../types";
import { PreviewNotice } from "./BinaryFallback";

// A document the browser renders on its own — an HTML report, a PDF — shown in a
// frame that loads it from the content origin. The bytes never enter this origin,
// which is the point: an agent-written report can carry anything.
//
// `content.sandboxed` decides the frame's powers and comes from the plan, not
// from this component. A sandboxed document gets an allow-list of at most
// `allow-scripts` (an HTML report, `content.scripts`): same-origin, forms,
// popups and top-level navigation stay withheld — an absent attribute would
// grant every one of them, and same-origin is what keeps the page in an opaque
// origin that can read nothing of the app or the content domain. A PDF is framed
// without the attribute because the browser's own viewer is the renderer, and a
// sandbox stops it from running.
//
// The content origin is a SECOND server on a second hostname, reached by the
// browser rather than by us, and a frame is the one preview whose failure we
// cannot see in a response: nothing here fetches anything. When that server does
// not answer, the frame shows the browser's own blank pane or its "refused to
// connect" page — neither of which says whose problem it is or what is left to
// do. A frame reports nothing we can read either: a cross-origin failure fires
// `load` like any other document (the browser's error page IS a document), and
// `error` is not delivered for a frame at all. The one signal there is, is that
// nothing happened — so the load is timed, and a frame that has not loaded by
// the end of it is reported as unreachable, with the two things still possible:
// try it again, or take the bytes.

/** How long a burst of writes must pause before the frame reloads. An agent
 *  rewriting a report writes it many times a second; reloading on every write
 *  leaves a strobing pane that cannot be read. */
const RELOAD_SETTLE_MS = 300;
/** How long "Updated" stays up after a reload. */
const PULSE_MS = 2000;
/** How long a frame may take before it is called unreachable. Generous on
 *  purpose: a large PDF over a slow link is not a broken server, and a notice
 *  thrown up over a preview that was about to arrive is the worse failure. */
export const FRAME_LOAD_TIMEOUT_MS = 15_000;

const UNREACHABLE = "The content server didn’t answer, so this preview couldn’t be loaded.";

interface Loaded {
  url: string;
  version: string;
}

/** Where this attempt is: waiting on the frame, drawn, or given up on. */
type Attempt = "loading" | "shown" | "unreachable";

export function FramePreview(props: PreviewProps): ReactElement {
  const { content, version, onDownload } = props;
  const url = content.kind === "frame" ? content.url : "";
  const [loaded, setLoaded] = useState<Loaded>({ url, version });
  const [pulsing, setPulsing] = useState(false);
  // Bumped to start the load over: the frame is keyed on it, so a retry
  // re-fetches even though the url and the version are unchanged.
  const [attemptNo, setAttemptNo] = useState(0);
  const [attempt, setAttempt] = useState<Attempt>("loading");

  useEffect(() => {
    if (version === loaded.version) return;
    const settle = setTimeout(() => {
      setLoaded({ url, version });
      setPulsing(true);
      // New bytes are a new attempt. A server that was down when the last
      // version was written is no reason to refuse to draw this one.
      setAttempt("loading");
    }, RELOAD_SETTLE_MS);
    return () => clearTimeout(settle);
  }, [url, version, loaded.version]);

  useEffect(() => {
    if (!pulsing) return;
    const fade = setTimeout(() => setPulsing(false), PULSE_MS);
    return () => clearTimeout(fade);
  }, [pulsing]);

  // One watchdog per attempt, cleared the moment the frame answers either way.
  useEffect(() => {
    if (attempt !== "loading") return;
    const giveUp = setTimeout(() => setAttempt("unreachable"), FRAME_LOAD_TIMEOUT_MS);
    return () => clearTimeout(giveUp);
  }, [attempt, loaded.version, attemptNo]);

  const retry = useCallback(() => {
    setAttemptNo((n) => n + 1);
    setAttempt("loading");
  }, []);

  if (content.kind !== "frame") {
    return <PreviewNotice>Nothing to show yet.</PreviewNotice>;
  }

  if (attempt === "unreachable") {
    // No dead frame beside the sentence: an empty pane under a refusal reads as
    // the preview, and a reader waits at it.
    return (
      <div className="alk-preview-frame alk-preview-frame--unreachable">
        <PreviewNotice>{UNREACHABLE}</PreviewNotice>
        <div className="alk-preview-frame__actions">
          <Button variant="secondary" fill="outline" size="sm" onClick={retry}>
            Try again
          </Button>
          <Button variant="secondary" fill="ghost" size="sm" onClick={onDownload}>
            Download
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="alk-preview-frame">
      {pulsing ? (
        <span className="alk-preview-frame__pulse" aria-live="polite">
          Updated
        </span>
      ) : null}
      <iframe
        // The key is the reload. New bytes usually arrive behind the SAME url —
        // a page grant is reused until it nears expiry — so only replacing the
        // frame re-fetches them. The attempt number rides it so a retry of an
        // unchanged document is a real second request.
        key={`${loaded.version}#${attemptNo}`}
        className="alk-preview-frame__frame"
        src={loaded.url}
        title={content.title}
        referrerPolicy="no-referrer"
        allow=""
        loading="lazy"
        onLoad={() => setAttempt("shown")}
        {...(content.sandboxed ? { sandbox: content.scripts ? "allow-scripts" : "" } : {})}
      />
    </div>
  );
}
