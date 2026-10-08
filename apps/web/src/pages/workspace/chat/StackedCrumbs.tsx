// The trail every stacked chat page carries in its header.
//
// The organ is the chat-ui design's, so it renders inside a `.chat-root` the same
// way the chat surfaces do; this wrapper owns that scope and the editor theme
// on behalf of the pages, which are otherwise plain @alkera/ui.

import { Breadcrumbs } from "@alkera/ui";
import "@alkera/ui/chat/styles";

import { useStackedCrumbs } from "./crumbTrail";
import { useHostDark } from "./useHostDark";
import "./stacked-crumbs.css";

export interface StackedCrumbsProps {
  /** What this page is called, the trail's last step. */
  current: string;
}

export function StackedCrumbs({ current }: StackedCrumbsProps) {
  const dark = useHostDark();
  const crumbs = useStackedCrumbs(current);
  return (
    <div className="chat-root chat-crumbs-crumbs" data-theme={dark ? "dark" : undefined}>
      <Breadcrumbs crumbs={crumbs} />
    </div>
  );
}
