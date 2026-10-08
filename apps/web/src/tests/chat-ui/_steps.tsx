// Builders for driving tool-step adapters through the package's one public
// door, resolveStep -- the same seam the receipt transcript resolves parts
// through, so every assertion here is on what that surface can observe.

import { render } from "@testing-library/react";

import type { ToolConversationPart } from "@alkera/chat-model";
import { resolveStep, type CardStep, type StepEnvironment } from "@alkera/ui";

let minted = 0;

export function toolPart(name: string, over: Partial<Omit<ToolConversationPart, "kind">> = {}): ToolConversationPart {
  minted += 1;
  return { id: `part-${minted}`, kind: "tool", callId: `call-${minted}`, name, state: "completed", ...over };
}

export function stepOf(part: ToolConversationPart, env?: StepEnvironment): CardStep {
  const step = resolveStep(part, env);
  if (!step) throw new Error(`resolveStep returned no step for ${part.name}`);
  return step;
}

/** Render a step's well interior inside the package's root scope. */
export function renderBody(step: CardStep): HTMLElement {
  return render(<div className="chat-root">{step.body}</div>).container;
}
