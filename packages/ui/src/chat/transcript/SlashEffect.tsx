// A slash command's effect as one quiet accountable line: the command, its
// outcome, and the figure it moved. In flight it carries the in-progress
// wording under the working sheen ("Compacting…"), or the dot beat when the
// effect has no wording of its own.

import { WorkingDots } from "../indicators";
import s from "./slash.module.css";

export function SlashEffect({
  command,
  outcome,
  data,
  pending,
  running,
}: {
  command: string;
  outcome: string;
  data: string;
  /** The in-flight wording ("Compacting…"); it turns into the outcome + data
   *  once the effect lands. */
  pending?: string;
  running?: boolean;
}) {
  return (
    <div className={s.slash} data-running={running ? "" : undefined}>
      <span className={`${s.cmd} chat-mono`}>{command}</span>
      {running ? (
        pending ? (
          <span className="chat-shimmer">{pending}</span>
        ) : (
          <WorkingDots />
        )
      ) : (
        <>
          <span>{outcome}</span>
          <span className={`${s.data} chat-num`}>{data}</span>
        </>
      )}
    </div>
  );
}
