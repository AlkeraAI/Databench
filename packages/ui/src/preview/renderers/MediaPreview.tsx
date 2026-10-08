import type { ReactElement } from "react";

import type { PreviewProps } from "../types";
import { PreviewNotice } from "./BinaryFallback";

// Video and audio, played from the object URL the host fetched, with the
// browser's own controls. Nothing plays until a person presses play: a preview
// that starts itself hijacks whatever room the laptop is in.

export function MediaPreview(props: PreviewProps): ReactElement {
  const { content, facts } = props;
  if (content.kind !== "blob") {
    return <PreviewNotice>Nothing to show yet.</PreviewNotice>;
  }

  const essence = facts.mime.split(";")[0]?.trim().toLowerCase() ?? "";
  return (
    <div className="alk-preview-media">
      {essence.startsWith("audio/") ? (
        <audio className="alk-preview-media__player" src={content.url} controls preload="metadata">
          {/* A browser that cannot play the type still says what the file is. */}
          {facts.name}
        </audio>
      ) : (
        <video className="alk-preview-media__player" src={content.url} controls preload="metadata">
          {facts.name}
        </video>
      )}
    </div>
  );
}
