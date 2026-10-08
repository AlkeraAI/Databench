import { Pill } from "@alkera/ui";

import { TopbarActions } from "../../app/Topbar";
import { useRealtimeStatus } from "./status";

/**
 * The one visible sign that live updates stopped. Renders nothing while a transport is
 * connected, connecting or briefly reconnecting; once a client has been trying for longer
 * than its grace window (`down`) a pill lands in the masthead's actions slot so the reader knows
 * the page is now refreshing on the polling fallback. Mounted once in the shell, so every page's
 * masthead carries it without per-page wiring.
 *
 * BOTH transports count. The event stream carries invalidations; the socket carries the chat
 * itself — tokens, asks, the transcript — and its pong timeout takes it down for up to 45 s
 * while the stream stays perfectly healthy. Reading only the stream left a reader watching an
 * answer stop with nothing on screen to say the page was no longer live.
 */
export function RealtimeStatusIndicator() {
  const down = useRealtimeStatus((s) => s.sse === "down" || s.ws === "down");
  if (!down) return null;
  return (
    <TopbarActions>
      <Pill tone="warning" dot role="status" className="alk-rt-pill">
        Live updates paused
      </Pill>
    </TopbarActions>
  );
}
