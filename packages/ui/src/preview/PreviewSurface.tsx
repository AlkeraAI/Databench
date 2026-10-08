import type { ReactElement } from "react";

import "./defaults";
import "./preview.css";
import { planPreviewWith } from "./registry";
import { BinaryFallback, PreviewNotice } from "./renderers/BinaryFallback";
import type { PreviewProps } from "./types";

// The one component every surface that shows a file renders — a tab beside a
// chat, the large modal on the Files page, the page a share link lands on. It
// asks the registry who draws this file and hands that renderer the bytes the
// host fetched, so the three surfaces can never disagree about a type.
//
// A status that is not bytes is drawn by the card rather than by the renderer: a
// file that is gone, still on its way back from the machine holding the folder,
// too large to fetch, or failed, is a sentence and a Download — never an empty
// pane a person has to interpret.

export interface PreviewSurfaceProps extends PreviewProps {
  /** Draw with this renderer rather than the one the type would get. A host
   *  that offers a file more than one way (its source, its rendered page) names
   *  the one it means; unset, the registry decides. */
  rendererId?: string;
}

export function PreviewSurface({ rendererId, ...props }: PreviewSurfaceProps): ReactElement {
  const { status } = props;

  if (status === "loading") {
    return (
      <div className="alk-preview">
        <PreviewNotice>Loading preview…</PreviewNotice>
      </div>
    );
  }

  if (status !== "ready") {
    return (
      <div className="alk-preview">
        <BinaryFallback {...props} />
      </div>
    );
  }

  const { renderer } = planPreviewWith(props.facts, rendererId);
  return (
    <div className="alk-preview" data-renderer={renderer.id}>
      {/* Above the bytes, not over them: the reader has to know the copy is
          older before reading it, and the renderer keeps its whole pane. */}
      {props.notice ? (
        <p className="alk-preview__stale" role="note">
          {props.notice}
        </p>
      ) : null}
      <renderer.Component {...props} />
    </div>
  );
}
