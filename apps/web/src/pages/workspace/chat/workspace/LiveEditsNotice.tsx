/**
 * Text a live document could not keep, offered back to the person who typed it.
 *
 * The live lane never drops an edit the server has not taken without saying
 * so: when it has to (the server refused the edit, the file stopped being
 * editable live, a restarted history could not carry it), the words come here,
 * with a way to copy them. One component for every live text surface, so the
 * offer reads the same wherever it is made.
 */

import { Button } from "@alkera/ui";
import { useState } from "react";

/** The action that copies the offered text. */
export const COPY_MY_TEXT = "Copy my text";

export interface LiveEditsNoticeProps {
  message: string;
  /** The text the reader would otherwise lose. */
  restorable?: string;
  onDismiss: () => void;
}

export function LiveEditsNotice({ message, restorable, onDismiss }: LiveEditsNoticeProps) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard
      ?.writeText(restorable ?? "")
      .then(() => setCopied(true))
      .catch(() => setCopied(false));
  };
  return (
    <div className="alk-live-edits-notice" role={restorable ? "alert" : "status"}>
      <span className="alk-live-edits-notice__message">{message}</span>
      {restorable ? (
        <Button size="sm" variant="secondary" fill="outline" onClick={copy}>
          {copied ? "Copied" : COPY_MY_TEXT}
        </Button>
      ) : null}
      <Button size="sm" variant="secondary" fill="outline" onClick={onDismiss}>
        Dismiss
      </Button>
    </div>
  );
}
