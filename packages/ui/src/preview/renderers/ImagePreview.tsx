import { IconArrowsMaximize } from "@tabler/icons-react";
import { useCallback, useState, type CSSProperties, type ReactElement, type WheelEvent } from "react";

import { Button } from "../../primitives/controls/Button";
import type { PreviewProps, PreviewViewSetting } from "../types";
import { useRendererViewState } from "../viewState";
import { BinaryFallback, PreviewNotice } from "./BinaryFallback";

// A raster image, drawn from the object URL the host fetched. The pane is how a
// person watches a chart being redrawn, so the zoom belongs to the PERSON, not to
// the file: a new version swaps the bytes and leaves the framing alone.

/** Below this the picture is a speck; above it, one pixel fills the pane. */
const MIN_SCALE = 0.1;
const MAX_SCALE = 8;
/** One wheel notch. Multiplicative, so a step out undoes a step in. */
const ZOOM_STEP = 1.15;

type Mode = "fit" | "actual";

interface Framing {
  mode: Mode;
  scale: number;
}

const FIT: Framing = { mode: "fit", scale: 1 };

/** Read back framing this renderer wrote earlier. Anything else — a sort column
 *  another renderer left behind, a stray string — starts fresh rather than
 *  throwing a pane away over it. */
function framingFrom(viewState: unknown): Framing {
  if (typeof viewState !== "object" || viewState === null) return FIT;
  const state = viewState as { mode?: unknown; scale?: unknown };
  if (state.mode !== "fit" && state.mode !== "actual") return FIT;
  const scale = typeof state.scale === "number" && state.scale > 0 ? state.scale : 1;
  return { mode: state.mode, scale: clamp(scale) };
}

function clamp(scale: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
}

function stageStyle(framing: Framing): CSSProperties {
  return { transform: `scale(${framing.scale})` };
}

/** What a host may draw for this renderer. Framing is not a flag, so both sides
 *  of the switch are spelled out: going back to the pane also drops whatever the
 *  wheel had zoomed to, which is what the renderer's own button does. */
export const IMAGE_VIEW_SETTINGS: readonly PreviewViewSetting[] = [
  {
    key: "mode",
    label: "Actual size",
    icon: <IconArrowsMaximize size={15} stroke={1.8} aria-hidden />,
    on: { mode: "actual", scale: 1 },
    off: { ...FIT },
  },
];

/** The url whose bytes the browser refused to decode, so a retry is not needed to
 *  know the answer — and so REPLACING the bytes clears the verdict on its own,
 *  which is what a new version of a file being watched has to do.
 *
 *  A name is not a format: an ELF renamed `.png` is sniffed as a picture by every
 *  layer that only reads the extension, and the `<img>` is the first thing to
 *  find out otherwise. Left alone it drew the browser's broken-image glyph and
 *  the alt text in a blank pane, which tells a reader nothing and offers them
 *  nothing. The card the surface already has for a file it cannot draw says what
 *  happened and holds the Download. */
function useDecodeFailure(props: PreviewProps): {
  failed: boolean;
  onError: () => void;
} {
  const [brokenUrl, setBrokenUrl] = useState<string | null>(null);
  const url = props.content.kind === "blob" ? props.content.url : null;
  return {
    failed: url !== null && brokenUrl === url,
    onError: () => setBrokenUrl(url),
  };
}

/** An image with the framing controls: fit the pane, or actual size, plus the
 *  wheel. */
export function ImagePreview(props: PreviewProps): ReactElement {
  const { content, facts } = props;
  const [framing, setFraming] = useRendererViewState(props, framingFrom);
  const decode = useDecodeFailure(props);
  const hosted = props.viewControls === "host";

  const onWheel = useCallback(
    (event: WheelEvent<HTMLDivElement>) => {
      const factor = event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
      setFraming((current) => ({ mode: "actual", scale: clamp(current.scale * factor) }));
    },
    [setFraming],
  );

  if (content.kind !== "blob") {
    return <PreviewNotice>Nothing to show yet.</PreviewNotice>;
  }
  if (decode.failed) return <BinaryFallback {...props} />;

  const fitted = framing.mode === "fit";
  return (
    <div className="alk-preview-image">
      {hosted ? null : (
        <div className="alk-preview-image__bar">
          <Button
            variant="secondary"
            fill="ghost"
            size="sm"
            aria-pressed={!fitted}
            onClick={() => setFraming(fitted ? { mode: "actual", scale: 1 } : FIT)}
          >
            Actual size
          </Button>
        </div>
      )}
      <div className="alk-preview-image__stage" data-testid="preview-image-stage" onWheel={onWheel}>
        <img
          className="alk-preview-image__img"
          src={content.url}
          alt={facts.name}
          data-mode={framing.mode}
          style={stageStyle(framing)}
          onError={decode.onError}
        />
      </div>
    </div>
  );
}

/** An SVG document, drawn as an IMAGE and nothing else. An `<img>` is the
 *  neutering: script, `foreignObject` and external fetches inside the document are
 *  inert, where inlining it or framing it on this origin would run them. It gets no
 *  zoom controls — the drawing scales with the pane on its own. */
export function SvgImagePreview(props: PreviewProps): ReactElement {
  const { content, facts } = props;
  const decode = useDecodeFailure(props);
  if (content.kind !== "blob") {
    return <PreviewNotice>Nothing to show yet.</PreviewNotice>;
  }
  if (decode.failed) return <BinaryFallback {...props} />;
  return (
    <div className="alk-preview-image alk-preview-image--plain">
      <div className="alk-preview-image__stage">
        <img
          className="alk-preview-image__img"
          src={content.url}
          alt={facts.name}
          data-mode="fit"
          onError={decode.onError}
        />
      </div>
    </div>
  );
}
