import type { ButtonHTMLAttributes, ReactNode } from "react";

import { Button, type ButtonFill, type ButtonSize, type ButtonVariant } from "../Button";
import { cx } from "../../cx";
import { CheckIcon, CopyIcon } from "../../icons";
import { useCopyToClipboard } from "../../../hooks";

export interface CopyButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onClick" | "value" | "children"> {
  /** The text written to the clipboard on click. */
  value: string;
  /** The label, held across the copy (it never swaps to the confirmation). */
  children: ReactNode;
  /** The polite screen-reader confirmation announced on copy (default "Copied"). */
  copiedLabel?: ReactNode;
  variant?: ButtonVariant;
  fill?: ButtonFill;
  size?: ButtonSize;
}

/**
 * CopyButton -- a labelled button that copies `value` on click. The label holds; only the leading
 * icon flips to a check for a moment (the standard confirmation), and a polite live region
 * announces the copy for screen readers. The icon-only field form is {@link CopyReading}.
 */
export function CopyButton({ value, children, copiedLabel = "Copied", className, ...rest }: CopyButtonProps) {
  const { copied, copy } = useCopyToClipboard();

  return (
    <Button
      {...rest}
      className={cx("alk-copybtn", className)}
      leftSection={copied ? <CheckIcon size={15} /> : <CopyIcon size={15} />}
      onClick={() => copy(value)}
    >
      {children}
      <span className="alk-copybtn__live" aria-live="polite" data-vh>
        {copied ? copiedLabel : ""}
      </span>
    </Button>
  );
}
