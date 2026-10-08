// The subagent as its own transcript block, never a group step.
//
// STANCE: a delegation returns a REPORT, so the block is a framed document
// rather than a tool card: who wrote it, what they were asked, then their
// report set to be read. The plan document is the sibling register, and the
// fold on the bottom edge is the one control over the body.
//
// The block wears the child's own avatar rather than a generic subtask glyph.
// A spawned agent is a someone: the mark comes from the product's seed hash
// (`hashPixelAvatarSeed`), drawn here on the chat-ui design's 5x5 mirrored
// grid. The shipped 3x3 avatar reads the same hash, so the two surfaces agree
// on identity even while their geometries differ.
//
// The trailing control opens the child's chat as a stacked page (a chat page
// with a back button to this one); it renders only when the host wires the
// seam.

import { useId, type ReactElement } from "react";

import type {
  SubagentActivity,
  ToolConversationPart,
} from "@alkera/chat-model";
import { hashPixelAvatarSeed } from "@alkera/chat-model";
import { resolveProviderMark, Text } from "../sharedUi";
import { IconMessage2, IconX } from "@tabler/icons-react";

import { DialogCaret } from "../dialog";
import { useDisclosure } from "../hooks";
import { Prose } from "../prose";
import { count, num, objectOutput, str } from "../tools/alkeraPayload";
import "./subagent.css";

const OPEN_CHAT = "Open chat";
const MARK_BOX = 14;

/** The avatar is a square grid; the right columns mirror the left, which is
 *  what makes a mark this small read as a face rather than as speckle. */
const AVATAR_SIDE = 5;
const AVATAR_HALF = Math.ceil(AVATAR_SIDE / 2);

/** One bit of the hash per cell, laid over the mirrored half. */
function PixelAvatar({ seed }: { seed: string }): ReactElement {
  const hash = hashPixelAvatarSeed(seed);
  const cells: boolean[] = [];
  for (let row = 0; row < AVATAR_SIDE; row += 1) {
    for (let column = 0; column < AVATAR_SIDE; column += 1) {
      const mirrored = column < AVATAR_HALF ? column : AVATAR_SIDE - 1 - column;
      cells.push(((hash >> (row * AVATAR_HALF + mirrored)) & 1) === 1);
    }
  }
  return (
    <span className="chat-subagent-avatar" aria-hidden="true">
      {cells.map((on, index) => (
        <span key={index} data-on={on ? "" : undefined} />
      ))}
    </span>
  );
}

/** Everything the block reads off the payload. */
interface SpawnView {
  agent: string;
  prompt: string;
  body: string;
  running: boolean;
  model: string;
  toolCalls: number | null;
  duration: number | null;
  failed: boolean;
  childSessionId: string;
}

/** Prefer the live child's readings while it works, then the settled receipt. */
function readings(
  live: SubagentActivity | undefined,
  stats: Record<string, unknown> | null,
): Pick<SpawnView, "model" | "toolCalls" | "duration"> {
  if (live)
    return {
      model: str(live.model),
      toolCalls: live.toolCount || null,
      duration: null,
    };
  return {
    model: str(stats?.model),
    toolCalls: num(stats?.tool_calls),
    duration: num(stats?.duration_seconds),
  };
}

function deriveSpawn(part: ToolConversationPart): SpawnView {
  const result = objectOutput(part.output);
  const stats =
    result?.stats && typeof result.stats === "object"
      ? (result.stats as Record<string, unknown>)
      : null;
  const running = part.state === "running" || part.state === "pending";
  const live = running ? part.childActivity : undefined;
  return {
    agent: str(part.input?.agent),
    prompt: str(part.input?.prompt),
    body: running ? str(live?.lastMessage) : str(result?.summary),
    running,
    ...readings(live, stats),
    failed: part.state === "error",
    // The seed the product uses: the child's session id when the harness bound
    // one, the call id otherwise, so a spawn always has a stable mark.
    childSessionId: part.childSessionId || str(result?.child_session_id),
  };
}

export interface SubagentBlockProps {
  part: ToolConversationPart;
  /** Opens the child's chat as a stacked page; without it the control does not
   *  render (a webview cannot navigate itself). */
  onOpenChat?: (childSessionId: string) => void;
}

export function SubagentBlock({
  part,
  onOpenChat,
}: SubagentBlockProps): ReactElement {
  // A delegation's report is a whole second transcript's worth of reading. It
  // arrives folded, so opening a chat shows the reader their own conversation
  // rather than a wall of someone else's; the heading above the fold already
  // says who ran, on what, and how it went. Their own fold outlives the chat
  // (see useDisclosure).
  const [open, setOpen] = useDisclosure(`subagent:${part.callId}`, false);
  const bodyId = useId();
  const {
    agent,
    prompt,
    body,
    running,
    model,
    toolCalls,
    duration,
    failed,
    childSessionId,
  } = deriveSpawn(part);
  const openLabel = agent ? `Open the ${agent} chat` : OPEN_CHAT;
  const lead = running ? "Running" : "Report from";
  const heading = agent
    ? `${lead} ${agent}`
    : running
      ? "Running a subagent"
      : "Subagent report";
  const foldSubject = running ? "the latest message" : "the full report";

  return (
    <section
      data-tool="spawn_agent"
      data-subagent-block=""
      data-status={failed ? "error" : running ? "running" : "done"}
      data-open={open ? "" : undefined}
      aria-label={heading}
    >
      <div className="chat-subagent-main">
        <div className="chat-subagent-who">
          <span className="chat-subagent-mark" aria-hidden="true">
            <PixelAvatar seed={childSessionId || part.callId} />
          </span>
          <Text
            className="chat-subagent-name"
            tooltip="truncate"
            tooltipLabel={heading}
          >
            {agent ? `${lead} ` : heading}
            {agent ? (
              <span className="chat-subagent-name__id">{agent}</span>
            ) : null}
          </Text>
          {failed ? (
            <span className="chat-subagent-failed">
              <IconX size={13} stroke={2.4} />
              Failed
            </span>
          ) : null}
          {onOpenChat ? (
            <button
              type="button"
              className="chat-subagent-open"
              title={openLabel}
              aria-label={openLabel}
              onClick={() => onOpenChat(childSessionId)}
            >
              <IconMessage2 size={14} stroke={1.7} />
              <span className="chat-subagent-open__label">{OPEN_CHAT}</span>
            </button>
          ) : null}
        </div>

        <p className="chat-subagent-readings">
          {model ? (
            <span className="chat-subagent-reading chat-subagent-model">
              {resolveProviderMark(model, { size: MARK_BOX })}
              <Text className="chat-subagent-model__id" tooltip="truncate">
                {model}
              </Text>
            </span>
          ) : null}
          {toolCalls !== null ? (
            <span className="chat-subagent-reading chat-subagent-calls">
              {count(toolCalls, "tool call", "tool calls")}
            </span>
          ) : null}
          {duration !== null ? (
            <span className="chat-subagent-reading chat-subagent-duration">
              {duration.toFixed(1)}s
            </span>
          ) : null}
        </p>

        {prompt ? (
          <Text as="p" className="chat-subagent-ask" tooltip="truncate">
            {prompt}
          </Text>
        ) : null}

        {part.errorText ? (
          <p className="chat-subagent-fail">
            <span aria-hidden="true">
              <IconX size={13} stroke={2.4} />
            </span>
            <span className="chat-subagent-fail__text">{part.errorText}</span>
          </p>
        ) : null}

        {/* The child's latest message becomes its final report without changing register. */}
        <div className="chat-subagent-report" id={bodyId}>
          {body ? (
            <Prose content={body} streaming={running} />
          ) : (
            <p className="chat-subagent-waiting">
              {running ? "Starting…" : "No report came back."}
            </p>
          )}
        </div>
      </div>

      <button
        type="button"
        className="chat-subagent-fold"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen(!open)}
      >
        {open ? `Hide ${foldSubject}` : `Show ${foldSubject}`}
        <DialogCaret className="chat-subagent-caret" open={open} />
      </button>
    </section>
  );
}
