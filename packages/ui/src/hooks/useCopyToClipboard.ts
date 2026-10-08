import { useCallback, useEffect, useRef, useState } from "react";

/**
 * The shared copy-confirmation behavior behind every copy affordance (CopyButton, CopyReading,
 * CodeBlock's corner button): write the value to the clipboard best-effort, flip `copied` for a
 * moment, then revert. A denied or absent clipboard never throws or rejects unhandled; the
 * confirmation still flips so the interaction never dead-ends. Copying again while confirmed
 * restarts the revert timer.
 */
export function useCopyToClipboard(resetMs = 1500): { copied: boolean; copy: (value: string) => void } {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = useCallback(
    (value: string) => {
      void navigator.clipboard?.writeText(value).catch(() => {});
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), resetMs);
    },
    [resetMs],
  );

  return { copied, copy };
}
