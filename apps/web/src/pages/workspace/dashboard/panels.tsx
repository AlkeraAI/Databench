import { Link } from "react-router-dom";

import { Card, Inline, Text } from "@alkera/ui";

import { Icon } from "../../../app/icons";
import shell from "./dashboard.module.css";
import styles from "./panels.module.css";
import type { ChatSummary } from "./model";

/**
 * Chats — the way back into work, and the way to start it. The start action is ALWAYS present: on a
 * seat with no chats it is the whole panel, so a first-run overview offers the next step instead of
 * placeholder copy explaining what is missing.
 */
export const NO_CHATS = "No chats yet.";

export function ChatsPanel({ chats }: { chats: ChatSummary[] }) {
  return (
    <Card
      className={shell.chats}
      title="Chats"
      actions={
        <Link to="/chat/new" className={`${styles.start} alk-link`}>
          <Inline as="span" gap={1} wrap={false}>
            <Icon name="plus" size={14} />
            Start a chat
          </Inline>
        </Link>
      }
      data-measure-col
    >
      {chats.length > 0 ? (
        <ul className={styles.chatList}>
          {chats.map((c) => (
            <li key={c.id}>
              {/* The title alone names the link — the "2h ago" beside it is a reading, not part of
                  what the row opens. */}
              <Link to={`/chat/${c.id}`} className={`${styles.chatRow} alk-rowlink`} aria-label={c.title}>
                <Text as="span" variant="name" className={styles.chatTitle}>
                  {c.title}
                </Text>
                <Text as="span" tone="muted">
                  {c.updated}
                </Text>
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        // A card with a title and nothing under it reads as one still loading.
        <Text as="p" tone="muted">
          {NO_CHATS}
        </Text>
      )}
    </Card>
  );
}
