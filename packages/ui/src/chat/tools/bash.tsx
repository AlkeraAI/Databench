// Bash: the card is the terminal the command ran in. The head carries the two
// facts a reader wants first -- whether it exited clean, and the last thing it
// printed. The well restates the invocation the way a shell shows it, working
// directory and prompt included, then hands over to the printout, which scrolls
// sideways because a run's column alignment is part of what it printed.

import type { ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";

import { highlightBash } from "../syntax";
import { readResult, str } from "./alkeraPayload";
import { stepStatus, type CardHead } from "./step";
import "./shared.css";
import "./bash.css";

interface Printout {
  output: string;
  exitCode: number | null;
  truncated: boolean;
}

/** The harness wraps merged stdout and stderr in a `BashResult`; a streaming run
 *  has only the raw text on `content`. The shell the model is offered is
 *  alkera's own, which lands that record as a JSON STRING, so this reads through
 *  the shared unwrap -- reading `part.output` directly printed the raw JSON into
 *  the terminal and lost the exit code. An opencode-native shell really does
 *  return bare text, and that parses to nothing and falls through. */
function printout(part: ToolConversationPart): Printout {
  const out = readResult(part.output);
  // Shape-checked, not merely parsed: a command whose stdout IS a JSON object
  // (`cat package.json`) parses too, and reading it as a record would blank the
  // terminal. A real record always carries the run's own fields.
  const record = out !== null && typeof out.output === "string" && ("exit_code" in out || "truncated" in out);
  if (record) {
    const code = out.exit_code;
    return {
      output: (out.output as string) || str(part.content),
      exitCode: typeof code === "number" ? code : null,
      truncated: out.truncated === true,
    };
  }
  return { output: str(part.output) || str(part.content), exitCode: null, truncated: false };
}

/** The directory the shell prompts from, shown the way a prompt shows it. */
function promptDir(workdir: string): string {
  const trimmed = workdir.replace(/\/+$/, "");
  const cut = trimmed.lastIndexOf("/");
  return cut === -1 ? trimmed : trimmed.slice(cut + 1);
}

/** The well's interior: the invocation the way a shell shows it, then what the
 *  run printed. It paints no ground, edge, radius, or outer pad; the group's
 *  well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const command = str(part.input?.command);
  const workdir = str(part.input?.workdir);
  const run = printout(part);
  return (
    <div data-tool="bash">
      <div className="chat-tool-band chat-tool-mono">
        {workdir ? (
          <span className="chat-bash-cwd" title={workdir}>
            {promptDir(workdir)}
          </span>
        ) : null}
        <span className="chat-bash-sigil" aria-hidden="true">
          $
        </span>
        <span className="chat-tool-band__text">{highlightBash(command)}</span>
      </div>
      {run.output ? <pre className="chat-bash-term chat-tool-mono">{run.output}</pre> : null}
    </div>
  );
}

/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part) => {
  const run = printout(part);
  // A nonzero exit is a failure the harness itself reports as a completed call.
  const failed = part.state === "error" || (run.exitCode !== null && run.exitCode !== 0);
  return {
    object: str(part.input?.command),
    data: run.exitCode === null ? undefined : { kind: "count", text: `exit ${run.exitCode}` },
    status: failed ? "error" : stepStatus(part.state),
    body: <Body part={part} />,
    footer: run.truncated ? "output truncated" : undefined,
  };
};
