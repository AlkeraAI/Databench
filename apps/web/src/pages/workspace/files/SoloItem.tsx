// One file, shared without the folder it lives in.
//
// The reader holds a grant on this node and on nothing above it, so there is no
// listing to draw: no siblings, no path, no folder above to walk to, and no
// create controls for a folder that is not theirs. What is left is the file
// itself and the two places they CAN go, which is what this card is.
//
// Everything the card does not draw is the point. The server already cuts a
// path at the deepest ancestor the reader may read, and this surface adds the
// second half of that promise: it renders the name and nothing that could carry
// an ancestor's name with it.

import { Link } from "react-router-dom";

import { Button } from "@alkera/ui";

import type { Item } from "@/api/files";

import { displayNameOf, formatSize, kindLabel, sizeOf } from "@/lib/files/columns";
import { granted } from "@/lib/capabilities";

export const SOLO_LINE = "Its folder isn't shared with you.";

export interface SoloItemProps {
  item: Item;
  /** Save the bytes. Offered only where the server says they may be had. */
  onDownload?: (item: Item) => void;
}

export function SoloItem({ item, onDownload }: SoloItemProps) {
  const downloadable = granted(item.capabilities?.can_download) && onDownload !== undefined;
  return (
    <div className="alk-files-solo">
      <h2 className="alk-files-solo__name">{displayNameOf(item)}</h2>
      <p className="alk-files-solo__facts">
        <span>{kindLabel(item)}</span>
        <span>{formatSize(sizeOf(item))}</span>
      </p>
      <p className="alk-files-solo__line">{SOLO_LINE}</p>
      <div className="alk-files-solo__ways">
        <Link className="alk-files-solo__way" to="/files">
          Home
        </Link>
        <Link className="alk-files-solo__way" to="/files?place=sharedWithMe">
          Shared with me
        </Link>
        {downloadable ? (
          <Button variant="secondary" fill="outline" onClick={() => onDownload(item)}>
            Download
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export default SoloItem;
