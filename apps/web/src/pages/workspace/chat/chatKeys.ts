// The one place a chat query key is spelled.
//
// One key per read. The chat list and the model catalog are each a SINGLE cache
// entry however many surfaces read them, so a delete, a rename, or a send
// refreshes what every surface is showing instead of half of them. A second key
// for the same read is the bug this module exists to prevent: it splits the
// cache, and every writer then has to remember to invalidate both.

const IDE = "ide";

export const chatKeys = {
  /** Every chat the daemon lists. Home, the sidecar, the chat route, and the
   *  crumb trail read this one entry. */
  chats: () => [IDE, "chat", "chats"] as const,

  /** A chat's folded transcript. The id rides the key verbatim, including the
   *  absent one a not-yet-routed page passes, so a page waiting for its param
   *  keeps its own entry rather than colliding with a real chat's. */
  turns: (chatId: string | null | undefined) => [IDE, "chat", "turns", chatId] as const,

  /** The gateway's model catalog for every composer, home and chat alike. */
  models: () => [IDE, "chat", "models"] as const,

  /** An open chat's model picker: every model with whether the chat may move
   *  to it. Refreshed after an accepted switch, and after one refused because
   *  the chat moved under the reader (409). */
  modelOptions: (chatId: string) => [IDE, "chat", "modelOptions", chatId] as const,

  /** The saved Default Chat Model + Effort, resolved against the live catalog. */
  chatDefaults: () => [IDE, "chatDefaults"] as const,

  /** The daemon's slash-command vocabulary. */
  commands: () => [IDE, "commands"] as const,

} as const;

/** The per-chat activity page: four independent reads of one chat's audit
 *  trail, each its own entry so saving caps refreshes only the ledger. */
export const activityKeys = {
  decisions: (chatId: string) => [IDE, "activity", "decisions", chatId] as const,
  safety: (chatId: string) => [IDE, "activity", "safety", chatId] as const,
  cost: (chatId: string) => [IDE, "activity", "cost", chatId] as const,
  ledger: (chatId: string) => [IDE, "activity", "ledger", chatId] as const,
} as const;
