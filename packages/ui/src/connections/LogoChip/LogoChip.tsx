// The square brand tile an integration is recognized by. ConnectorMark owns every
// drawing while this component owns the tile chrome.
//
// Shared because both surfaces name the same integrations: the VS Code webview's
// connections list and the browser portal's preconfigured-connections plate.

import { ConnectorMark } from "../../brand";

import "./logo-chip.css";

export interface LogoChipProps {
  pluginId: string;
  /** Edge length of the square chip in px. */
  size: number;
}

/** A square neutral tile holding the integration's real brand mark in its own brand colour.
 *  An integration with no brand mark gets a generic plug, signalling a connectable source. */
export function LogoChip({ pluginId, size }: LogoChipProps) {
  const glyphSize = Math.round(size * 0.56);
  return (
    <span
      className="alk-logochip"
      style={{ width: size, height: size }}
      aria-hidden="true"
    >
      <ConnectorMark id={pluginId} size={glyphSize} />
    </span>
  );
}
