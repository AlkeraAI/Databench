// How the notebook names whoever did something: one rule for the cell footer,
// the run queue, carets and notices.
//
// A person is their display name. An agent working for a person is "<agent>
// for <that person>", otherwise its own name or the agent name. The platform
// itself is the system name. The open build says "Agent" and "Databench"; a
// product names both through `nameActors` during composition. The server always
// sends a person's name; when it sends an empty one the label says "A former
// member". An id is never shown.

import type { ActorRef } from "./types";

/** The label for a member whose name the server could not give. */
export const FORMER_MEMBER = "A former member";

export interface ActorNames {
  /** The platform, when it runs something itself. */
  readonly system: string;
  /** An agent that works for nobody in particular. */
  readonly agent: string;
}

/** The open build's names. */
export const OPEN_ACTOR_NAMES: ActorNames = { system: "Databench", agent: "Agent" };

let actorNames: ActorNames = OPEN_ACTOR_NAMES;

/** Name the platform and its agent. A product calls it once, before the first render. */
export function nameActors(names: ActorNames): void {
  actorNames = names;
}

/** The names the notebook shows for the platform and its agent. */
export function currentActorNames(): ActorNames {
  return actorNames;
}

const name = (v: string | null | undefined): string => (typeof v === "string" ? v.trim() : "");

/** Who an actor is, as the notebook shows them. */
export function actorLabel(actor: Pick<ActorRef, "kind" | "display_name" | "acting_for"> | null | undefined): string {
  if (!actor) return actorNames.system;
  if (actor.kind === "system") return actorNames.system;
  if (actor.kind === "agent") {
    const person = name(actor.acting_for?.display_name);
    if (person) return `${actorNames.agent} for ${person}`;
    return name(actor.display_name) || actorNames.agent;
  }
  return name(actor.display_name) || FORMER_MEMBER;
}

