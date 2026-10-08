// Where a stacked page sits in the stack, read straight off the route.
//
// A drill-in already carries its origin in `?backTo=`, and that origin is the
// FULL location it was opened from, its own `backTo` included. So one route
// encodes the whole ancestry: `/editor/blobs/b?backTo=/editor/chat/b?backTo=
// /chat/a` is Chats > chat a > chat b > Results. Walking that chain is the
// trail, and no separate history has to be kept in sync with it.
//
// A level whose path this module cannot name is left out rather than shown as
// an anonymous crumb; its own ancestors stay in the trail.

import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import type { ConversationTurn } from "@alkera/chat-model";
import type { Crumb } from "@alkera/ui";

import { spawnChildrenOf } from "./data/harnessEventFold";

import { chatKeys } from "./chatKeys";
import { chatData } from "./data";
import { useTranscriptLookup } from "./useTranscriptLookup";

export type CrumbKind = "home" | "chat" | "plan" | "compaction" | "results" | "result" | "activity";

export interface CrumbStep {
  /** The router target this level lives at, its own trail still attached. */
  target: string;
  kind: CrumbKind;
  /** The chat this level belongs to, when its path names one. */
  chatId: string | null;
}

/** A deep chain is a corrupt one: the drill-ins that exist go three or four
 *  levels, so anything past this is a loop or a hand-edited route. */
const MAX_DEPTH = 8;

const KIND_LABELS: Record<Exclude<CrumbKind, "chat">, string> = {
  home: "Chats",
  plan: "Plan",
  compaction: "Compaction",
  results: "Results",
  result: "Result",
  activity: "Chat activity",
};

function decode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/** The `backTo` this target was opened from, or null at the root of a trail. */
function originOf(target: string): string | null {
  const query = target.indexOf("?");
  if (query < 0) return null;
  const raw = new URLSearchParams(target.slice(query)).get("backTo");
  return raw?.startsWith("/") ? raw : null;
}

export function stepOf(target: string): CrumbStep | null {
  const pathname = target.split("?")[0] ?? target;
  const parts = pathname.split("/").filter(Boolean).map(decode);
  if (parts[0] === "sidecar") return { target, kind: "home", chatId: null };
  if (parts[0] === "chat") return { target, kind: "chat", chatId: parts[1] ?? null };
  if (parts[0] !== "editor") return null;
  const chatId = parts[2] ?? null;
  switch (parts[1]) {
    case "chat":
      return { target, kind: "chat", chatId };
    case "plan":
      return { target, kind: "plan", chatId };
    case "compaction":
      return { target, kind: "compaction", chatId };
    case "blobs":
      return { target, kind: "results", chatId };
    case "blob":
      return { target, kind: "result", chatId };
    case "activity":
      return { target, kind: "activity", chatId };
    default:
      return null;
  }
}

/** The trail for `location`, root first and the current page last.
 *
 *  Two levels are implicit in the routes rather than in a `backTo`, and both
 *  are filled in here so the trail never starts mid-stack: home, which a chat
 *  opened from the sidebar has no link back to, and the chat a detail page
 *  names in its own path but was deep-linked to without a trail. */
export function crumbSteps(location: string): CrumbStep[] {
  const chain: string[] = [];
  const seen = new Set<string>();
  let cursor: string | null = location;
  while (cursor && chain.length < MAX_DEPTH && !seen.has(cursor)) {
    seen.add(cursor);
    chain.push(cursor);
    cursor = originOf(cursor);
  }
  const steps = chain
    .reverse()
    .map(stepOf)
    .filter((step): step is CrumbStep => step !== null);
  if (steps[0]?.kind !== "home") steps.unshift({ target: "/sidecar", kind: "home", chatId: null });

  const trail: CrumbStep[] = [];
  for (const step of steps) {
    const previous = trail[trail.length - 1];
    const owner = step.chatId;
    const owned = previous?.kind === "chat" && previous.chatId === owner;
    if (owner && step.kind !== "chat" && !owned) {
      trail.push({ target: `/chat/${encodeURIComponent(owner)}`, kind: "chat", chatId: owner });
    }
    trail.push(step);
  }
  return trail;
}

export function stepLabel(step: CrumbStep, titles: Map<string, string>): string {
  if (step.kind !== "chat") return KIND_LABELS[step.kind];
  if (!step.chatId) return "New chat";
  return titles.get(step.chatId) ?? "Chat";
}

/** One press up a level: the trail's last-but-one step, so the back button and
 *  the trail can never point a reader two different ways. A page deep-linked
 *  with no trail still climbs to the chat its own path names, then home. */
export function useStackedBack(): () => void {
  const location = useLocation();
  const navigate = useNavigate();
  const target = `${location.pathname}${location.search}`;
  return useCallback(() => {
    const steps = crumbSteps(target);
    navigate(steps[steps.length - 2]?.target ?? "/sidecar");
  }, [navigate, target]);
}

/**
 * The crumbs a stacked page hands to `<Breadcrumbs>`: every level above it
 * clickable, and the level it is last. Chat names come from the list the chat
 * surfaces already cache, so the trail costs no extra round trip. A page whose
 * name is its own to state (a plan, a result) passes `current`; a chat leaves it
 * out and is named the way every other chat in the trail is.
 */
export function useStackedCrumbs(current?: string): Crumb[] {
  const location = useLocation();
  const navigate = useNavigate();
  const target = `${location.pathname}${location.search}`;
  // The chat list is a press away from every surface that carries a trail, so
  // spending the trail's first slot on it costs width and buys nothing.
  const steps = useMemo(() => crumbSteps(target).filter((step) => step.kind !== "home"), [target]);
  const chatsQuery = useQuery({
    queryKey: chatKeys.chats(),
    queryFn: () => chatData().listChats(),
  });
  const chats = chatsQuery.data;
  // A chat level the list cannot name is a subagent session: the sidebar list
  // holds root chats only. The chat ABOVE it is the one whose spawn card named
  // it, so that chat's transcript is what the trail has to read -- the same
  // query its own surface runs. Keyed off the first unnamed level rather than
  // the last, since a drill-in can sit below the level needing the name.
  const { ownerId, childId } = useMemo(() => {
    const named = new Set((chats ?? []).map((chat) => chat.id));
    const chatLevels = steps.filter((step) => step.kind === "chat" && step.chatId);
    const orphan = chatLevels.findIndex((step) => !named.has(step.chatId as string));
    return orphan > 0
      ? { ownerId: chatLevels[orphan - 1].chatId, childId: chatLevels[orphan].chatId }
      : { ownerId: null, childId: null };
  }, [chats, steps]);
  // The spawn card that named this child can be far above the page the parent
  // chat opens on, so the read walks up until it holds the card rather than
  // labelling the level with a session id.
  const until = useCallback(
    (turns: ConversationTurn[]) =>
      childId === null || spawnChildrenOf(turns).some((link) => link.childSessionId === childId),
    [childId],
  );
  const ownerTurns = useTranscriptLookup(ownerId, { enabled: Boolean(ownerId), until }).turns;

  return useMemo(() => {
    const titles = new Map<string, string>((chats ?? []).map((chat) => [chat.id, chat.title]));
    for (const link of spawnChildrenOf(ownerTurns)) titles.set(link.childSessionId, link.label);
    return steps.map((step, index) =>
      index === steps.length - 1
        ? { label: current ?? stepLabel(step, titles) }
        : { label: stepLabel(step, titles), onGo: () => navigate(step.target) },
    );
  }, [chats, current, navigate, ownerTurns, steps]);
}
