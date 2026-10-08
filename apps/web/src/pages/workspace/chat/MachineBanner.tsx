// What the org's workspace machine is doing, said plainly above the chat.
//
// The rule this exists for: a chat must never hang silently. If the machine is
// still coming up, or has stopped answering, or was never started, or is running
// but may not publish THIS chat, the reader is told which of those it is and what
// happens next — and the composer is DISABLED rather than hidden, so the
// affordance stays where it was and the reason for it being unavailable is on
// screen beside it.
//
// A ready machine gets no chrome at all: a banner that is always there is
// furniture, and stops being read.

import type { ReactElement } from "react";

import {
  Button,
  StatusPill,
  statusNeedsSaying,
  stepProgress,
  type MachineCardData,
} from "@alkera/ui";

import type { ChatMachineStatus } from "../../../api/cloudChat/transport";
import { machineBilling } from "../../../app/extensions/portal";
import type { StatusFact } from "../../../api/status";

/** What the pinned machine's banner falls back on, and what the page says the
 *  moment the box's socket goes away, before the server's next read can.
 *  Every other state's words are the server's ({@link StatusFact}). */
export const MACHINE_COPY = {
  starting: {
    title: "The machine is starting…",
    body: "Your message is sent as soon as the machine is ready.",
  },
  unreachable: {
    title: "The machine is unreachable",
    body: "Reconnecting…",
  },
} as const;

/** What a slept chat says for itself: the box is live and holds no session
 *  for it. Not an error and not a wait — opening the chat has already asked
 *  the box to take it back, and a message wakes it the same way. */

/** Whether the reader may compose. A slept chat is composable: the send is
 *  exactly what wakes it, and taking the composer away would leave the reader
 *  with no way to do the one thing that brings the chat back. */
export function machineIsReady(status: ChatMachineStatus | undefined): boolean {
  // `draining` is composable for the same reason `asleep` is: the box is
  // answering, and a chat it will not keep is one placement moves on the very
  // next message. Taking the composer away would stop the send that moves it.
  // `restarting` is the same box coming back with the same chats: a message
  // sent now is taken by the new process the moment it is up.
  // `pool` is the org's next chat going to a shared box the create places it on
  // at once: the send is what makes the chat, so it must be possible.
  return (
    status === "ready" ||
    status === "asleep" ||
    status === "pool" ||
    status === "draining" ||
    status === "restarting"
  );
}

/** What stands above the chat: the status the server wrote, when it has
 *  something to say (a reason, or something wrong). The one thing said from
 *  here is the box's socket going away, which this browser sees most of a
 *  minute before the machine's heartbeat window can tell the server; it is
 *  drawn only until the server's own word arrives. A healthy or resting chat
 *  gets no chrome at all: a banner that is always there stops being read. */
export function MachineBanner({
  status,
  fact,
}: {
  status: ChatMachineStatus | undefined;
  fact: StatusFact | null | undefined;
}): ReactElement | null {
  if (statusNeedsSaying(fact)) {
    return <StatusPill status={fact} variant="line" className="chat-machine-banner" />;
  }
  if (status !== "unreachable") return null;
  return (
    <div className="chat-machine-banner" role="status" data-status={status}>
      <strong className="chat-machine-banner__title">{MACHINE_COPY.unreachable.title}</strong>
      <span className="chat-machine-banner__body">{MACHINE_COPY.unreachable.body}</span>
    </div>
  );
}

// ---- a workspace pinned to an org machine ------------------------------------

/** What the banner offers beside a pinned machine's state. */
/** A pinned banner's control: the open ones, or one an installed extension offers (its
 *  label and effect come from the extension). */
export type PinnedAction = "try_again" | "change_machine" | (string & {});

export interface PinnedCopy {
  state: string;
  title: string;
  body?: string;
  actions: PinnedAction[];
  /** A state that needs nothing but saying: drawn as the quiet line. */
  quiet?: boolean;
  /** A state a sent message does not clear (a failed start, no free hardware,
   *  or a stop an extension names): the chat's own "a message wakes it" line would
   *  contradict the banner, so it is not drawn. */
  blocksWake?: boolean;
}

/** The pinned machine's banner, from its card: what it is doing, in its own
 *  name, and the way on where there is one. `manager` is whether the reader may
 *  start it, `mayAddCredits` whether they may add credits (an org admin; an
 *  installed billing extension reads it), `canMove` whether they may move the
 *  workspace off it. A stop an installed extension names (`stop_reason`) is
 *  worded by it. A running machine says nothing. */
export function pinnedMachineCopy(
  card: Pick<
    MachineCardData,
    | "kind"
    | "name"
    | "state"
    | "step"
    | "step_started_at"
    | "step_expected_seconds"
    | "stop_reason"
    | "spec"
    | "drain_stops_at"
    | "fault"
  >,
  { manager, mayAddCredits = manager, canMove, now }: { manager: boolean; mayAddCredits?: boolean; canMove: boolean; now: number },
): PinnedCopy | null {
  if (card.kind !== "org_machine") return null;
  const name = card.name;
  const move: PinnedAction[] = canMove ? ["change_machine"] : [];
  const extended = machineBilling()?.pinnedStopCopy(card, { manager, mayAddCredits });
  if (extended) return extended;
  switch (card.state) {
    case "starting": {
      const progress = stepProgress(card.step_started_at, card.step_expected_seconds, now);
      return {
        state: "starting",
        title: `Starting ${name}.`,
        body: progress
          ? `About ${progress.minutesLeft} ${progress.minutesLeft === 1 ? "minute" : "minutes"}.`
          : MACHINE_COPY.starting.body,
        actions: [],
      };
    }
    case "waiting_for_hardware": {
      const gpu = card.spec?.gpu?.name;
      return {
        state: "waiting_for_hardware",
        title: gpu ? `No ${gpu} is free right now.` : `No hardware is free for ${name} right now.`,
        body: "We keep trying.",
        actions: move,
        blocksWake: true,
      };
    }
    case "stopping":
      return null;
    case "stopped":
      if (card.stop_reason === "not_responding") {
        return {
          state: "stopped_not_responding",
          title: `${name} stopped because it wasn't responding.`,
          body: "Sending a message starts it again.",
          actions: move,
        };
      }
      return { state: "stopped", title: `${name} is stopped.`, body: "Sending a message starts it.", actions: [], quiet: true };
    case "failed":
      return {
        state: "failed",
        title: `${name} couldn't start.`,
        actions: [...(manager ? (["try_again"] as PinnedAction[]) : []), ...move],
        blocksWake: true,
      };
    case "unreachable":
      return { state: "unreachable", title: `${name} isn't responding.`, body: MACHINE_COPY.unreachable.body, actions: [] };
    case "unhealthy":
      return { state: "unhealthy", title: `${name} can't run chats.`, body: card.fault?.message, actions: move, blocksWake: true };
    default:
      return null;
  }
}

const ACTION_LABEL: Record<string, string> = {
  try_again: "Try again",
  change_machine: "Change machine",
};

/** A control's label: the open one, or the installed extension's. */
const actionLabel = (action: PinnedAction): string =>
  ACTION_LABEL[action] ?? machineBilling()?.pinnedActions[action]?.label ?? action;

/** The banner for a pinned machine's state, drawn from {@link pinnedMachineCopy}. */
export function PinnedMachineBannerView({
  copy,
  onAction,
}: {
  copy: PinnedCopy;
  onAction: (action: PinnedAction) => void;
}): ReactElement {
  return (
    <div
      className={copy.quiet ? "chat-machine-banner chat-machine-banner--quiet" : "chat-machine-banner"}
      role="status"
      data-status={copy.state}
    >
      <strong className="chat-machine-banner__title">{copy.title}</strong>
      {copy.body ? <span className="chat-machine-banner__body">{copy.body}</span> : null}
      {copy.actions.length > 0 ? (
        <span className="chat-machine-banner__actions">
          {copy.actions.map((action) => (
            <Button key={action} size="sm" variant="secondary" className="chat-machine-banner__action" onClick={() => onAction(action)}>
              {actionLabel(action)}
            </Button>
          ))}
        </span>
      ) : null}
    </div>
  );
}
