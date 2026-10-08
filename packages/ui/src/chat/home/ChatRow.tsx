// One row of the chat home: what the chat is called, how fresh it is, what it
// is waiting on, and what can be done to it. The three-dot menu is present at
// rest, because a row's actions are not something to hunt for under a pointer.
//
// A chat that spawned subagents fuses with them, while open, into one framed
// cluster. A subagent can spawn its own: the one frame holds the whole family,
// and each deeper level hangs from a thread under its parent's mark column.

import { useState, type ReactElement, type ReactNode } from "react";

import { Text } from "../sharedUi";
import {
  IconArrowUpRight,
  IconChevronDown,
  IconFileDescription,
  IconKey,
  IconQuestionMark,
  IconTrash,
} from "@tabler/icons-react";

import { Spinner } from "../indicators/Indicators";
import { MenuRow, OverflowGlyph, PopMenu } from "../panel";

/** What the row's mark reports. An ask outranks it: a chat stopped for the
 *  human is not working, whatever its turn says. */
export type ChatRowStatus = "working" | "finished" | "idle";

/** The ask that stopped the turn. Each wears its own glyph, so a reader knows
 *  what is wanted of them before opening the chat. */
export type ChatAskKind = "permission" | "question" | "plan";

export interface SubchatRowView {
  id: string;
  title: string;
  /** How long ago the chat last did something, already worded ("12m"). */
  freshness?: string;
  status: ChatRowStatus;
  ask?: ChatAskKind;
  /** For a finished chat: whether the user has read it since. Read steps the
   *  dot down to the quiet reading blue; unread keeps the vibrant done blue. */
  read?: boolean;
  /** The subagent chats this chat spawned, each free to carry its own. */
  children?: readonly SubchatRowView[];
}

/** A root chat row. Same shape as a subchat's; the root alone wears the
 *  actions menu. */
export type ChatRowView = SubchatRowView;

const WORKING = "The agent is working";
const FINISHED = "The agent finished this turn";
// Named for what it does, not for where it lands: this chrome renders in a
// browser tab as well as in the editor, and "Open chat in editor" offered a
// reader with no editor a surface they do not have.
const OPEN_CHAT = "Open chat";
const DELETE_CHAT = "Delete chat";
const HIDE_SUBS = "Hide subagent chats";

function showSubsLabel(count: number): string {
  return count === 1 ? "Show 1 subagent chat" : `Show ${count} subagent chats`;
}

const ASK_LABEL: Record<ChatAskKind, string> = {
  permission: "Waiting for permission",
  question: "Waiting for your answer",
  plan: "Waiting for plan approval",
};

/** A key to grant, a mark to answer, a document to approve. */
const ASK_GLYPH: Record<ChatAskKind, ReactNode> = {
  permission: <IconKey size={13} stroke={1.8} aria-hidden />,
  question: <IconQuestionMark size={13} stroke={2.2} aria-hidden />,
  plan: <IconFileDescription size={13} stroke={1.8} aria-hidden />,
};

function StatusMark({
  status,
  ask,
  read,
}: {
  status: ChatRowStatus;
  ask?: ChatAskKind;
  read?: boolean;
}): ReactNode {
  if (ask) {
    return (
      <span className="chat-hrow__ask" data-kind={ask} role="img" aria-label={ASK_LABEL[ask]} title={ASK_LABEL[ask]}>
        {ASK_GLYPH[ask]}
      </span>
    );
  }
  if (status === "working") {
    return (
      <span className="chat-hrow__work" role="img" aria-label={WORKING} title={WORKING}>
        <Spinner />
      </span>
    );
  }
  if (status === "finished") {
    return (
      <span
        className="chat-hrow__done"
        data-read={read ? "true" : undefined}
        role="img"
        aria-label={FINISHED}
        title={FINISHED}
      />
    );
  }
  return null;
}

/** The disclosure: the subagent count and its caret ride one capsule, the one
 *  control on the row that owns the cluster. */
/** Both row tiers wear the same child-count disclosure. */
function SubsBadge({ count, open, onToggle }: { count: number; open: boolean; onToggle: () => void }): ReactElement | null {
  return count > 0 ? <CountBadge count={count} open={open} onToggle={onToggle} /> : null;
}

function CountBadge({
  count,
  open,
  onToggle,
}: {
  count: number;
  open: boolean;
  onToggle: () => void;
}): ReactElement {
  const label = open ? HIDE_SUBS : showSubsLabel(count);
  return (
    <button
      type="button"
      className="chat-hrow__count"
      aria-expanded={open}
      aria-label={label}
      title={label}
      onClick={onToggle}
    >
      <span>{count}</span>
      <IconChevronDown size={8} stroke={2.4} aria-hidden />
    </button>
  );
}

/** One row's full-width strip, and the whole strip is the chat's door. The
 *  mark+title button is the door's focusable form; it carries no handler of
 *  its own, because its activation click bubbles to the strip's, which serves
 *  every pixel of the line. The capsule and the menu outrank the strip: a
 *  press landing inside either acts there and never also opens the chat.
 *  Focus order follows the DOM -- open button, capsule, menu trigger. A line
 *  without the menu reserves its box, so the freshness holds one right axis
 *  across every row. */
function RowLine({
  row,
  lineClass,
  openClass,
  disclosure,
  actions,
  onOpen,
}: {
  row: SubchatRowView;
  lineClass: string;
  openClass: string;
  disclosure?: ReactNode;
  actions?: ReactNode;
  onOpen: () => void;
}): ReactElement {
  return (
    <div
      className={lineClass}
      onClick={(event) => {
        if (event.target instanceof Element && event.target.closest(".chat-hrow__count, .chat-menu")) return;
        onOpen();
      }}
    >
      <button type="button" className={openClass}>
        <span className="chat-hrow__mark">
          <StatusMark status={row.status} ask={row.ask} read={row.read} />
        </span>
        <Text className="chat-hrow__title" tooltip="truncate">
          {row.title}
        </Text>
      </button>
      {disclosure}
      <span className="chat-hrow__spacer" />
      {row.freshness ? <span className="chat-hrow__fresh chat-num">{row.freshness}</span> : null}
      {actions ?? <span className="chat-hrow__menu-slot" aria-hidden="true" />}
    </div>
  );
}

/** A subagent row inside the cluster. One with children repeats the capsule;
 *  its own subagents step in once more and hang from the thread. */
function SubRow({ row, onOpen }: { row: SubchatRowView; onOpen: (id: string) => void }): ReactElement {
  const [open, setOpen] = useState(false);
  const subs = row.children ?? [];
  return (
    <li className="chat-hrow">
      <RowLine
        row={row}
        lineClass="chat-hrow__sline"
        openClass="chat-hrow__sopen"
        disclosure={<SubsBadge count={subs.length} open={open} onToggle={() => setOpen((v) => !v)} />}
        onOpen={() => onOpen(row.id)}
      />
      {open && subs.length > 0 ? (
        <ul className="chat-hrow__subs2">
          {subs.map((sub) => (
            <SubRow key={sub.id} row={sub} onOpen={onOpen} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export interface ChatRowProps {
  row: ChatRowView;
  onOpen: (id: string) => void;
  onOpenInEditor?: (id: string) => void;
  onDelete?: (id: string) => void;
}

export function ChatRow({ row, onOpen, onOpenInEditor, onDelete }: ChatRowProps): ReactElement {
  const [open, setOpen] = useState(false);
  const subs = row.children ?? [];
  const clustered = open && subs.length > 0;

  const actions = onOpenInEditor || onDelete ? (
    <PopMenu
      className="chat-hrow__menu"
      triggerClassName="chat-hrow__more"
      label={`Actions for ${row.title}`}
      glyph={<OverflowGlyph />}
    >
      {(close) => (
        <>
          {onOpenInEditor ? (
            <MenuRow
              id="openInEditor"
              icon={<IconArrowUpRight size={13} stroke={1.8} aria-hidden />}
              label={OPEN_CHAT}
              onSelect={() => {
                close();
                onOpenInEditor(row.id);
              }}
            />
          ) : null}
          {onDelete ? (
            <MenuRow
              id="delete"
              icon={<IconTrash size={13} stroke={1.8} aria-hidden />}
              label={DELETE_CHAT}
              danger
              onSelect={() => {
                close();
                onDelete(row.id);
              }}
            />
          ) : null}
        </>
      )}
    </PopMenu>
  ) : null;

  return (
    <li className={clustered ? "chat-hrow chat-hrow--cluster" : "chat-hrow"}>
      <RowLine
        row={row}
        lineClass="chat-hrow__line"
        openClass="chat-hrow__open"
        disclosure={<SubsBadge count={subs.length} open={open} onToggle={() => setOpen((v) => !v)} />}
        actions={actions}
        onOpen={() => onOpen(row.id)}
      />
      {clustered ? (
        <ul className="chat-hrow__subs">
          {subs.map((sub) => (
            <SubRow key={sub.id} row={sub} onOpen={onOpen} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}
