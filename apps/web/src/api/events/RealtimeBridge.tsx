import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { onBrowserOnline } from "@/lib/online";

import { withOrgAssertion } from "../activeOrg";
import { useCurrentUser } from "../auth";
import { apiBaseUrl, fireUnauthorized, refreshSession } from "../client";
import { createInvalidationScheduler, parseFrame } from "./eventMap";
import { publishFrame } from "./frameBus";
import { SseClient, type FetchLike, type SseStatus } from "./sseClient";
import { useRealtimeStatus } from "./status";

/**
 * Bridges the server event stream to the query cache. While a user is signed in, one
 * `SseClient` holds `GET /api/v1/events` open; every frame it delivers becomes a coalesced
 * cache invalidation through the event map, a `reset` frame (the server dropped events for
 * this subscriber, or its cursor was unusable) invalidates everything, and a stream that comes
 * back after having been `down` invalidates everything once — the cursor replays the gap, and
 * this is the insurance for the one case the cursor cannot cover (a row that committed with a
 * lower id while nobody was listening). A refusal on the stream takes the same one silent
 * refresh an ordinary request gets, and only if that fails drops the cached identity through
 * the same reaction every other transport uses, so the guards bounce to login. Renders nothing.
 *
 * `fetch` is a seam for tests; the shipping app uses the browser's.
 */
export function RealtimeBridge({ fetch }: { fetch?: FetchLike }) {
  const queryClient = useQueryClient();
  const signedIn = useCurrentUser().data != null;

  useEffect(() => {
    const store = useRealtimeStatus.getState();
    if (!signedIn) {
      store.setSse("idle");
      return;
    }
    const scheduler = createInvalidationScheduler(queryClient);
    let previous: SseStatus = "idle";
    const client = new SseClient({
      url: `${apiBaseUrl}/api/v1/events`,
      // The stream names the org this tab rendered, like every other request: a
      // tab left behind after a switch elsewhere reloads instead of listening to
      // the new org's events.
      fetch,
      wrapFetch: withOrgAssertion,
      onFrame: (frame) => {
        if (frame.type === "reset") {
          scheduler.reset();
          return;
        }
        const parsed = parseFrame(frame.type, frame.data);
        if (!parsed) return;
        // The frame itself first, then what it invalidates: a surface watching
        // for its own node (a live folder, an open file tab) reacts to the frame
        // it named, and the coalesced cache pass follows behind it.
        publishFrame(parsed);
        scheduler.push(parsed);
      },
      onStatus: (status) => {
        if (status === "connected" && previous === "down") scheduler.reset();
        previous = status;
        useRealtimeStatus.getState().setSse(status);
      },
      refresh: refreshSession,
      onUnauthorized: fireUnauthorized,
    });
    client.start();
    // The browser saying the network is back is the moment to try, not the end
    // of a backoff that kept growing while every attempt was bound to fail.
    const unfollow = onBrowserOnline({ online: () => client.retryNow() });
    return () => {
      unfollow();
      client.stop();
      scheduler.dispose();
      useRealtimeStatus.getState().setSse("idle");
    };
  }, [queryClient, signedIn, fetch]);

  return null;
}
