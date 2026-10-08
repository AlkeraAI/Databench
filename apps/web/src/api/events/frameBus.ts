// A second reader for the frames the event stream delivers.
//
// The event map answers "what may now be stale" and the scheduler refetches it.
// That is enough for a screen whose freshness is a cache question, and not
// enough for one whose behaviour depends on the frame itself: a folder mounted
// live has to know that THIS folder moved (and not a sibling) so it can refresh
// exactly that listing, and a node open in a tab has to know its own node
// changed so it can re-read one item instead of a family.
//
// So the bridge publishes every frame here before it invalidates anything, and
// a caller subscribes with the predicate that names the frames it cares about.
// The bus holds no state and never fetches: it is the delivery, and the
// subscriber decides what a frame means. A listener that throws is contained —
// one broken subscriber must not cost the others their frame, nor stop the
// invalidation that follows.

import { useEffect, useRef } from "react";

import type { RealtimeEventFrame } from "./eventMap";

export type FramePredicate = (frame: RealtimeEventFrame) => boolean;
export type FrameListener = (frame: RealtimeEventFrame) => void;

interface Subscription {
  predicate: FramePredicate;
  listener: FrameListener;
}

const subscriptions = new Set<Subscription>();

/**
 * Receive every frame `predicate` accepts, until the returned function is
 * called. Subscribing twice with the same listener yields two subscriptions,
 * each with its own unsubscribe — there is no identity to collapse.
 */
export function subscribeFrames(predicate: FramePredicate, listener: FrameListener): () => void {
  const subscription: Subscription = { predicate, listener };
  subscriptions.add(subscription);
  return () => {
    subscriptions.delete(subscription);
  };
}

/**
 * Hand one frame to everyone who asked for it. Called by the realtime bridge;
 * a test drives it directly.
 *
 * The set is copied first so a listener that subscribes or unsubscribes while
 * being called cannot change who else hears this frame, and each call is
 * isolated so a predicate or a listener that throws costs only itself.
 */
export function publishFrame(frame: RealtimeEventFrame): void {
  for (const subscription of [...subscriptions]) {
    // A subscription removed by an earlier listener in this same pass must not
    // still be called.
    if (!subscriptions.has(subscription)) continue;
    try {
      if (subscription.predicate(frame)) subscription.listener(frame);
    } catch {
      // Contained on purpose: the stream is shared infrastructure.
    }
  }
}

/** Drop every subscription — a test's `afterEach`. */
export function resetFrameBus(): void {
  subscriptions.clear();
}

/**
 * The React form: subscribe for the life of the component.
 *
 * Both arguments are read through a ref, so a caller may pass inline closures
 * (the normal case — a predicate closes over the ids it is watching) without
 * re-subscribing on every render. `deps` re-arms nothing; it exists only so a
 * caller can force a fresh subscription when it genuinely wants one.
 */
export function useFrames(predicate: FramePredicate, listener: FrameListener): void {
  const current = useRef({ predicate, listener });
  current.current = { predicate, listener };
  useEffect(
    () =>
      subscribeFrames(
        (frame) => current.current.predicate(frame),
        (frame) => current.current.listener(frame),
      ),
    [],
  );
}
