import type { KeyboardEvent, MouseEvent } from "react";
import { Tooltip } from "../../..";
import { useDisplayPath } from "./WorkspacePaths";

export interface PathDisplayProps {
  path: string;
  displayPath?: string;
  onOpen?: () => void;
  className?: string;
}

/** Render a path with middle-truncation-friendly spans. Visible text is the
 * workspace-relative form from the surrounding `WorkspacePathsProvider` (an
 * explicit `displayPath` wins); the copy span, tooltip and aria keep `path`. */
export function PathDisplay({ path, displayPath, onOpen, className }: PathDisplayProps) {
  const contextDisplay = useDisplayPath();
  const visiblePath = displayPath ?? contextDisplay(path);
  const { start, end } = splitPathForMiddleEllipsis(visiblePath);
  const inner = (
    <>
      <span className="alk-path-ellipsis__copy">{path}</span>
      <span className="alk-path-ellipsis__start" aria-hidden>{start}</span>
      <span className="alk-path-ellipsis__end" aria-hidden>{end}</span>
    </>
  );
  const classes = ["alk-path-ellipsis", onOpen ? "alk-path-ellipsis--link" : "", className].filter(Boolean).join(" ");

  const open = (event: MouseEvent | KeyboardEvent) => {
    event.preventDefault();
    event.stopPropagation();
    onOpen?.();
  };
  return (
    <Tooltip label={path} wrap maxWidth="min(420px, calc(100vw - 24px))" openDelay={180}>
      {(tooltip) => (
        <span
          ref={tooltip.ref}
          className={classes}
          role={onOpen ? "link" : undefined}
          tabIndex={onOpen ? 0 : undefined}
          aria-label={onOpen ? `Open ${path}` : path}
          aria-describedby={tooltip["aria-describedby"]}
          onPointerEnter={tooltip.onPointerEnter}
          onPointerLeave={tooltip.onPointerLeave}
          onFocus={tooltip.onFocus}
          onBlur={tooltip.onBlur}
          onClick={onOpen ? open : undefined}
          onKeyDown={
            onOpen
              ? (event) => {
                  if (event.key === "Enter" || event.key === " ") open(event);
                }
              : undefined
          }
        >
          {inner}
        </span>
      )}
    </Tooltip>
  );
}

export const MiddleEllipsisPath = PathDisplay;

function splitPathForMiddleEllipsis(path: string): { start: string; end: string } {
  const normalized = path.replace(/\\/g, "/");
  const segments = normalized.split("/");
  if (segments.length <= 2) return { start: "", end: normalized };
  const endSegments = segments.slice(-2).join("/");
  return { start: normalized.slice(0, normalized.length - endSegments.length), end: endSegments };
}
