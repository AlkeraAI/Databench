import { cx } from "../../../primitives/cx";
import { Markdown } from "../../../primitives/render";
import "./CompactionDetail.css";

export interface CompactionTextPanelProps {
  /** The full compaction summary — `CompactionConversationPart.text`. */
  text: string;
  className?: string;
}

/** Render the full compaction summary in an editor-tab detail surface. */
export function CompactionTextPanel({ text, className }: CompactionTextPanelProps) {
  return (
    <div className={cx("alk-compaction", "alk-scroll", className)}>
      <Markdown content={text} />
    </div>
  );
}
