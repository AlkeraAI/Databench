import { IconTextWrap } from "@tabler/icons-react";
import { useCallback, type ReactElement } from "react";

import { Button } from "../../primitives/controls/Button";
import type { PreviewProps, PreviewViewSetting } from "../types";
import { useRendererViewState } from "../viewState";
import { PreviewNotice } from "./BinaryFallback";
import { MoreWindowBar, useMoreWindow } from "./MoreWindow";

// A plain-text file, shown as the characters it holds. React puts the body in as
// a text node, so markup in the file stays markup on the screen — interpreting it
// would run whatever an agent, or whoever handed the agent the file, wrote there.
//
// Long lines are a real choice: a log wants no wrap and a prose file wants one, so
// the toggle is the person's and the host keeps it across a remount. Where the
// host has taken the control into its own chrome the bar here is not drawn — one
// row of controls, not two.

/** Read back a wrap this renderer wrote earlier; anything else starts unwrapped.
 *  The shape, not the flag, is what the host is handed back — a renderer's view
 *  state is an object so a second setting can join it without changing the
 *  contract. */
export function wrapFrom(viewState: unknown): { wrap: boolean } {
  if (typeof viewState !== "object" || viewState === null) return { wrap: false };
  return { wrap: (viewState as { wrap?: unknown }).wrap === true };
}

/** What a host may draw for this renderer. */
export const TEXT_VIEW_SETTINGS: readonly PreviewViewSetting[] = [
  { key: "wrap", label: "Soft wrap", icon: <IconTextWrap size={15} stroke={1.8} aria-hidden /> },
];

export function TextPreview(props: PreviewProps): ReactElement {
  const { content } = props;
  const [{ wrap }, setWrap] = useRendererViewState(props, wrapFrom);
  const hosted = props.viewControls === "host";
  const tail = useMoreWindow(content);

  const toggle = useCallback(
    () => setWrap((current) => ({ wrap: !current.wrap })),
    [setWrap],
  );

  if (content.kind !== "text") {
    return <PreviewNotice>Nothing to show yet.</PreviewNotice>;
  }

  return (
    <div className="alk-preview-text" onScrollCapture={tail.onScrollCapture}>
      {hosted ? null : (
        <div className="alk-preview-text__bar">
          <Button variant="secondary" fill="ghost" size="sm" aria-pressed={wrap} onClick={toggle}>
            Soft wrap
          </Button>
        </div>
      )}
      <pre
        className="alk-preview-text__body"
        data-testid="preview-text-body"
        data-wrap={wrap ? "on" : "off"}
      >
        {content.text}
      </pre>
      <MoreWindowBar tail={tail} />
    </div>
  );
}
