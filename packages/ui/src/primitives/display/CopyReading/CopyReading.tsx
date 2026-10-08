import type { HTMLAttributes } from "react";

import { Button } from "../../controls/Button";
import { cx } from "../../cx";
import { CheckIcon, CopyIcon } from "../../icons";
import { useCopyToClipboard } from "../../../hooks";

export interface CopyReadingProps extends HTMLAttributes<HTMLDivElement> {
  /** The reading itself — shown in the mono voice and written to the clipboard on copy. */
  value: string;
  /** The faint brass frame for a precious reading (an org id, a once-shown secret). */
  precious?: boolean;
  /** Value size — `md` (default) is the 14px reading; `sm` the compact 12px one (a long URL). */
  size?: "sm" | "md";
  /** Accessible name for the copy button. Defaults to "Copy". */
  copyLabel?: string;
}

/** The copyable mono reading: a bordered control-ground box holding a truncating mono value and an
 *  icon-only ghost copy button. Copying flips the glyph to a check for a moment and announces
 *  "Copied" politely; `precious` swaps the hairline for the brass frame. Labels and descriptions
 *  stay OUTSIDE (Field owns them). */
export function CopyReading({ value, precious, size = "md", copyLabel, className, ...rest }: CopyReadingProps) {
  const { copied, copy } = useCopyToClipboard();

  return (
    <div
      className={cx("alk-copyreading", className)}
      // md is the default — only sm emits the attribute (mirrors Button's size convention).
      data-size={size !== "md" ? size : undefined}
      data-precious={precious ? "" : undefined}
      data-copied={copied ? "" : undefined}
      {...rest}
    >
      <code className="alk-copyreading__val alk-code alk-truncate">{value}</code>
      <Button
        iconOnly
        size="sm"
        variant="secondary"
        fill="ghost"
        aria-label={copyLabel ?? "Copy"}
        onClick={() => copy(value)}
      >
        {copied ? <CheckIcon size={15} /> : <CopyIcon size={15} />}
      </Button>
      <span className="alk-copyreading__live" aria-live="polite" data-vh>
        {copied ? "Copied" : ""}
      </span>
    </div>
  );
}
