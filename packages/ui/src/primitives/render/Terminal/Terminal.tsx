import { Fragment, type CSSProperties, type ReactNode } from "react";

import { cx } from "../../cx";
import { tokenizeShell } from "../highlight";

export interface TerminalProps {
  /** The shell command to render as a prompt line (`<cwd> $ <command>`), shell-highlighted. */
  command?: string;
  /** The working directory shown before the prompt sigil. Omit for a bare `$`. */
  cwd?: string;
  /** The command's output, rendered as plain mono text below the prompt. */
  output?: string;
  /** Soft-wrap long lines instead of scrolling (default true — a terminal wraps). */
  wrap?: boolean;
  /** Cap the output height; it scrolls past this. A number is px. */
  maxHeight?: number | string;
  /** Accessible name for the region. */
  "aria-label"?: string;
  className?: string;
  /** Arbitrary content in place of / below command+output (e.g. streamed rich lines). */
  children?: ReactNode;
}

/** Render one shell command as a highlighted prompt line: `<cwd> $ <command>`. */
function PromptLine({ command, cwd }: { command: string; cwd?: string }) {
  const segs = tokenizeShell(command);
  return (
    <div className="alk-terminal__line alk-terminal__line--command">
      <span className="alk-terminal__prompt" aria-hidden="true">
        {cwd ? <span className="alk-terminal__cwd">{cwd}</span> : null}
        <span className="alk-terminal__sigil">$</span>
      </span>{" "}
      {segs.map((s, i) =>
        s.kind ? (
          <span className={`alk-terminal__sh alk-terminal__sh--${s.kind}`} key={i}>
            {s.text}
          </span>
        ) : (
          <Fragment key={i}>{s.text}</Fragment>
        ),
      )}
    </div>
  );
}

/**
 * Terminal — a shell prompt + output plate.
 *
 * The command renders as `<cwd> $ <command>` with lightweight shell highlighting (command name, flags,
 * strings, variables); the output renders as plain mono text below. The theme is Alkera's warm palette
 * derived from the semantic tokens, so it tracks light/dark and — inside VS Code — adopts the editor's
 * terminal background/foreground and font. The sibling of CodeBlock: CodeBlock is grammar-highlighted
 * source, Terminal is a command and its output.
 */
export function Terminal({
  command,
  cwd,
  output,
  wrap = true,
  maxHeight,
  className,
  children,
  ...rest
}: TerminalProps) {
  const style: CSSProperties | undefined =
    maxHeight != null ? { ["--alk-terminal-max" as string]: typeof maxHeight === "number" ? `${maxHeight}px` : maxHeight } : undefined;

  return (
    <div
      className={cx("alk-terminal", wrap && "alk-terminal--wrap", className)}
      style={style}
      role="group"
      aria-label={rest["aria-label"] ?? "Terminal"}
    >
      {command ? <PromptLine command={command} cwd={cwd} /> : null}
      {output ? (
        <pre className="alk-terminal__line alk-terminal__line--output" tabIndex={0}>
          {output}
        </pre>
      ) : null}
      {children}
    </div>
  );
}
