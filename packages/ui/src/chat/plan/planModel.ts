import type { QuestionConversationPart } from "@alkera/chat-model";

export const PLAN_REJECT_ANSWER = "The user rejected this plan. Revise it and present an updated plan.";

export function planRejected(part: QuestionConversationPart): boolean {
  if (part.status === "rejected") return true;
  return part.answers?.some((answer) => answer.includes(PLAN_REJECT_ANSWER)) ?? false;
}

export function planAcceptedMode(label: string): string {
  const lower = label.toLowerCase();
  if (lower.includes("bypass")) return "bypass";
  if (lower.includes("auto mode") || lower.includes("run automatically")) return "auto";
  return "default";
}

export function planAcceptDisplay(label: string): string {
  const lower = label.toLowerCase();
  if (lower.includes("bypass")) return "Accept with bypassing permissions";
  if (lower.includes("auto mode") || lower.includes("run automatically")) {
    return "Accept with auto mode (pause for risky steps)";
  }
  if (lower.includes("run normally") || lower.includes("default")) return "Accept with default permissions";
  return label;
}
