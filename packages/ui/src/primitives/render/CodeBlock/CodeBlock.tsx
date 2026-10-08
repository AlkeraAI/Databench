import { Fragment, type CSSProperties, type ReactNode } from "react";

import { Button } from "../../controls/Button";
import { cx } from "../../cx";
import { CheckIcon, CopyIcon } from "../../icons";
import { useCopyToClipboard } from "../../../hooks";
import { type CodeSeg, langForPath, tokenizeInline, tokenizeToLines } from "../highlight";

export interface CodeBlockProps {
  /** The source to render. For an object, pretty-print it first (`JSON.stringify(v, null, 2)`). */
  code: string;
  /** Explicit Prism grammar id — "json", "sql", "python", "typescript", "bash", "yaml", "markdown"… */
  language?: string;
  /** Infer the grammar from a file path/name ("model.sql") when `language` is absent. */
  path?: string;
  /** Show the line-number gutter (default true). Off for a short inline-ish snippet. */
  lineNumbers?: boolean;
  /** Reading size. `sm` (default) is the dense snippet size; `md` is the roomier reading size for a
   *  plate a user studies and copies (a workflow file, a config the page hands them). */
  size?: "sm" | "md";
  /** Soft-wrap long lines instead of scrolling the plate horizontally (default false). */
  wrap?: boolean;
  /** Cap the plate height; it scrolls past this. A number is px. */
  maxHeight?: number | string;
  /** A copy button in the top-right corner (at the code's own padding). `none` (default) omits it;
   *  `always` keeps it visible; `hover` reveals it on hover / keyboard focus of the plate. */
  copyButton?: "none" | "always" | "hover";
  /** Accessible name for the scrollable region. */
  "aria-label"?: string;
  className?: string;
}

function renderSegs(segs: CodeSeg[], keyPrefix: string): ReactNode {
  if (segs.length === 0) return " ";
  return segs.map((s, i) =>
    s.classes ? (
      <span className={s.classes} key={`${keyPrefix}-${i}`}>
        {s.text}
      </span>
    ) : (
      <Fragment key={`${keyPrefix}-${i}`}>{s.text}</Fragment>
    ),
  );
}

/** The top-right copy affordance: an icon-only button that flips to a check for a moment and
 *  announces the copy politely. */
function CodeCopyButton({ code }: { code: string }) {
  const { copied, copy } = useCopyToClipboard();
  return (
    <Button
      className="alk-codeblock-frame__copy"
      iconOnly
      variant="secondary"
      fill="ghost"
      size="sm"
      aria-label={copied ? "Copied" : "Copy code"}
      onClick={() => copy(code)}
    >
      {copied ? <CheckIcon size={15} /> : <CopyIcon size={15} />}
    </Button>
  );
}

/**
 * CodeBlock — a syntax-highlighted code plate.
 *
 * Prism owns the grammar; the theme is Alkera's warm palette (olive keywords, clay strings, graphite
 * ink), derived from the semantic tokens so it tracks light/dark and — inside VS Code — adopts the
 * editor's own background/foreground and font. Pass a language (or a `path` to infer one) and a string
 * of code; JSON gets pretty-printed by the caller. Renders to classed spans, never dangerous HTML.
 */
export function CodeBlock({
  code,
  language,
  path,
  lineNumbers = true,
  size = "sm",
  wrap = false,
  maxHeight,
  copyButton = "none",
  className,
  ...rest
}: CodeBlockProps) {
  const lang = language ?? (path ? langForPath(path) : null);
  const style: CSSProperties | undefined =
    maxHeight != null ? { maxHeight: typeof maxHeight === "number" ? `${maxHeight}px` : maxHeight } : undefined;
  const dataSize = size !== "sm" ? size : undefined;

  const preEl = !lineNumbers ? (
    <pre
      className={cx("alk-codeblock", "alk-codeblock--flush", wrap && "alk-codeblock--wrap", className)}
      style={style}
      data-size={dataSize}
      tabIndex={0}
      aria-label={rest["aria-label"]}
    >
      <code className="alk-codeblock__code">{renderSegs(tokenizeInline(code, lang), "s")}</code>
    </pre>
  ) : (
    <pre
      className={cx("alk-codeblock", wrap && "alk-codeblock--wrap", className)}
      style={style}
      data-size={dataSize}
      tabIndex={0}
      aria-label={rest["aria-label"]}
    >
      <code className="alk-codeblock__code">
        {tokenizeToLines(code, lang).map((segs, i) => (
          <span className="alk-codeblock__line" key={i}>
            <span className="alk-codeblock__n" aria-hidden="true">
              {i + 1}
            </span>
            <span className="alk-codeblock__t">{renderSegs(segs, `l${i}`)}</span>
          </span>
        ))}
      </code>
    </pre>
  );

  if (copyButton === "none") return preEl;
  return (
    <div className={cx("alk-codeblock-frame", copyButton === "hover" && "alk-codeblock-frame--hover")}>
      {preEl}
      <CodeCopyButton code={code} />
    </div>
  );
}
