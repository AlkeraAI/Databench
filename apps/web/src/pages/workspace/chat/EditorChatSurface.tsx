import { useParams } from "react-router-dom";
import { ChatSurface } from "./ChatSurface";

/**
 * Renders the chat surface inside an editor webview tab. The chat fills the
 * entire pane via flex; no centred reading column.
 */
export function EditorChatSurface() {
  const { id } = useParams();
  return <ChatSurface chatId={id} />;
}
