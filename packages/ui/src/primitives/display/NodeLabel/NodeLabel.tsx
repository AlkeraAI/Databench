import { cx } from "../../cx";
import { HomeIcon } from "../../icons";
import { nodeLabel, type NodeLabelSource } from "./labels";

export interface NodeLabelProps {
  node: NodeLabelSource;
  /** The label as the host escapes names for display; defaults to the raw label. */
  format?: (text: string) => string;
  /** Size of the home mark, in px. */
  iconSize?: number;
  className?: string;
}

/**
 * NodeLabel — a Files node's name as a person reads it: a member's home as the home
 * mark beside its owner's name, an object as its title, anything else as its name.
 *
 * Every surface that shows a node's name renders this (or reads `nodeLabel` for a
 * plain-text slot such as a tab title), so a home never shows the id it is stored
 * under. Pure presentation: the host hands in the node it already holds.
 */
export function NodeLabel({ node, format, iconSize = 14, className }: NodeLabelProps) {
  const label = nodeLabel(node);
  const text = format ? format(label.text) : label.text;
  return (
    <span className={cx("alk-node-label", className)} data-home={label.home ? "true" : undefined}>
      {label.home ? <HomeIcon size={iconSize} className="alk-node-label__mark" /> : null}
      <span className="alk-node-label__text">{text}</span>
    </span>
  );
}
