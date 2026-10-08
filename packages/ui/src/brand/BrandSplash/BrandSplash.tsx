// BrandSplash, the full-bleed cold-start screen for the extension sidebar.
// Shown while the daemon spawns / auth resolves (notably the first-run cold
// start). The brand's standalone mark over a caption, plus an
// optional determinate progress bar while the managed runtime downloads. Pure
// presentation; the host owns the phase text + fraction and passes them in.

import "./splash.css";

import { BrandLogo, currentBrand } from "../brand";
import { Meter } from "../../primitives/display/Meter";

export interface BrandSplashProps {
  /** Caption under the mark. Defaults to "Starting <product>…". */
  caption?: string;
  /** Determinate progress, 0..1. Omit for phases with no known fraction
   *  (extracting / starting) — the caption shows alone, with no bar. */
  progress?: number;
}

export function BrandSplash({ caption, progress }: BrandSplashProps) {
  const shown = caption ?? `Starting ${currentBrand().productName}…`;
  const showBar = progress != null && Number.isFinite(progress);
  const fraction = showBar ? Math.max(0, Math.min(1, progress)) : 0;
  return (
    <div className="alk-splash">
      <BrandLogo size={40} title="" />
      {/* Live region announces the PHASE (infrequent), not the % — a per-tick
        * percentage in a live region would spam a screen reader; the bar below
        * carries the numeric reading via aria-valuenow. */}
      <span className="alk-splash__caption" role="status">
        {shown}
      </span>
      {showBar ? (
        <div className="alk-splash__progress">
          <Meter
            className="alk-splash__bar"
            value={fraction}
            tone="brand"
            size="md"
            // A STATIC name, not the phase caption — the caption already lives in
            // the role="status" region above; naming the bar with it too would
            // make assistive tech announce the phase twice. The bar's numeric
            // reading rides aria-valuenow.
            label="Download progress"
          />
          <span className="alk-splash__percent" aria-hidden="true">
            {Math.round(fraction * 100)}%
          </span>
        </div>
      ) : null}
    </div>
  );
}
