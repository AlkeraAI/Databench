// Repo clone: the result is a handful of `Key: value` lines naming the label the
// reference resolved to, whether the managed cache was reused or refetched,
// where it landed, and the branch and commit it now sits at. The head's verb is
// that status word, because "cloned it" and "reused what was already here" are
// different answers to the same call and only the verb can say which happened.
import type { ReactElement } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { Text, type TextProps } from "../sharedUi";
import { IconGitFork } from "@tabler/icons-react";
import { field, objectOutput, str } from "./alkeraPayload";
import { Band, EmptyLine, LeafPath } from "./shared";
import type { CardHead, StepFigure } from "./step";
import "./shared.css";
import s from "./repo_clone.module.css";
interface Clone {
  repository: string;
  status: string;
  path: string;
  branch: string;
  head: string;
}
const VERBS: Record<string, string> = { cloned: "Cloned", refreshed: "Refreshed", cached: "Reused" };
const SOURCES: Record<string, string> = {
  cloned: "Cloned from the remote",
  refreshed: "Refreshed from the remote",
  cached: "Reused the cached copy",
};
/** The tool answers in `Key: value` lines; a harness can also land the same
 *  facts as the metadata object, so both spellings are read. */
function readClone(part: ToolConversationPart): Clone {
  const result = objectOutput(part.output);
  const text = (str(part.output) || str(part.content)).split("\n");
  return {
    repository: str(part.input?.repository) || str(result?.repository) || field(text, "repository ready"),
    status: str(result?.status) || field(text, "status"),
    path: str(result?.path) || str(result?.local_path) || field(text, "local path"),
    branch: str(part.input?.branch) || str(result?.branch) || field(text, "branch"),
    head: str(result?.head) || field(text, "head"),
  };
}
/** A commit reads by its first seven characters everywhere else, so it does here. */
function shortHead(head: string): string {
  return head.length > 7 ? head.slice(0, 7) : head;
}
/** The value is the element that clips, so the tip belongs to it and not to the
 *  row around it. */
function Fact({
  label,
  tooltip,
  tooltipLabel,
  children,
}: {
  label: string;
  tooltip?: TextProps["tooltip"];
  tooltipLabel?: string;
  children: ReactElement | string;
}): ReactElement {
  return (
    <div className="chat-tool-row">
      <dt className={s.cloneFactLabel}>{label}</dt>
      <Text as="dd" className={s.cloneFactValue} tooltip={tooltip} tooltipLabel={tooltipLabel}>
        {children}
      </Text>
    </div>
  );
}
/** The well's interior: the reference the call named, then where that landed and
 *  at what commit. It paints no ground, edge, radius, or outer pad; the group's
 *  well owns those. */
function RepoBand({ clone }: { clone: Clone }): ReactElement | null {
  if (!clone.repository) return null;
  return (
    <Band>
      <span className="chat-tool-band__icon" aria-hidden="true">
        <IconGitFork size={14} stroke={1.6} />
      </span>
      <Text className="chat-tool-band__text chat-tool-band__text--one chat-tool-mono" tooltip="truncate" tooltipLabel={clone.repository}>
        <LeafPath path={clone.repository} />
      </Text>
      {clone.branch ? <span className="chat-tool-band__param">on {clone.branch}</span> : null}
    </Band>
  );
}
function Facts({ clone }: { clone: Clone }): ReactElement {
  const short = shortHead(clone.head);
  return (
    <dl className={s.cloneFacts}>
      {clone.status ? (
        <Fact label="Source">
          <span className={s.cloneSource} data-status={clone.status}>
            {SOURCES[clone.status] ?? clone.status}
          </span>
        </Fact>
      ) : null}
      {clone.path ? (
        <Fact label="Path" tooltip="truncate" tooltipLabel={clone.path}>
          <span className="chat-tool-mono">
            <LeafPath path={clone.path} />
          </span>
        </Fact>
      ) : null}
      {clone.head ? (
        // A commit is abbreviated in code, never by the layout, so there is no
        // clipping to measure: the rest of the hash is reachable only on hover.
        <Fact label="Commit" tooltip={short === clone.head ? "none" : "always"} tooltipLabel={clone.head}>
          <span className="chat-tool-mono">{short}</span>
        </Fact>
      ) : null}
    </dl>
  );
}
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const clone = readClone(part);
  const bare = !clone.status && !clone.path && !clone.head;
  return (
    <div data-tool="repo_clone">
      <RepoBand clone={clone} />
      {bare ? <EmptyLine>The clone reported no details.</EmptyLine> : <Facts clone={clone} />}
    </div>
  );
}
/** What the caller ends up pointed at: the branch it asked for, or the commit
 *  that branch resolved to. */
function pinned(clone: Clone): StepFigure | undefined {
  const text = clone.branch || shortHead(clone.head);
  return text ? { kind: "count", text } : undefined;
}
/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part) => {
  const clone = readClone(part);
  return {
    verb: VERBS[clone.status],
    object: clone.repository,
    data: pinned(clone),
    body: <Body part={part} />,
  };
};
