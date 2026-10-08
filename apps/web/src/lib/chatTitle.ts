/** What a chat the server has not titled yet is called. */
export const UNTITLED_CHAT = "Untitled chat";

/** What a chat is called on screen. */
export function chatTitle(chat: { title?: string | null }): string {
  return chat.title || UNTITLED_CHAT;
}
