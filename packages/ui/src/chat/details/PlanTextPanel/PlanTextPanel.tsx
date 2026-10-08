import { cx } from "../../../primitives/cx";
import { Markdown } from "../../../primitives/render";
import "./PlanTextPanel.css";

export interface PlanTextPanelProps {
  /** The plan, as Markdown. */
  plan: string;
  className?: string;
}

/** Render a full approval plan as Markdown in a scrollable editor-tab surface. */
export function PlanTextPanel({ plan, className }: PlanTextPanelProps) {
  return (
    <div className={cx("alk-planview", "alk-scroll", className)}>
      <Markdown content={plan} />
    </div>
  );
}
