// Pure option and card-prop mapping for the chat-ui frontend: permission-mode
// vocabulary, plan-approval choices, the permission dock card, and the
// composer's option lists. No JSX; the transcript mapping lives in entries.tsx.

import type {
  AskCall,
  ComposerCommand,
  PermissionConversationPart,
  QuestionConversationPart,
  QuestionPromptView,
  SubjectFormat,
} from "@alkera/chat-model";
import {
  EXEC_TITLE,
  PERMISSION_MODES,
  alwaysScope,
  askFromPart,
  effortLabel,
  presentPermission,
  requestedBy,
} from "@alkera/chat-model";
import { PLAN_REJECT_ANSWER, planAcceptedMode, planRejected } from "@alkera/ui";
import type {
  EffortOption,
  ModeOption,
  PermissionCardProps,
  PlanModeChoice,
  PlanResolution,
  SlashCommand,
} from "@alkera/ui";
import { PERMISSION_MODE_OPTIONS } from "./adapters";
import { APPROVAL_REACHES, type ApprovalVerdict } from "./data/ChatDataSource";

/** The compact labels the registry gives the stances too long for the trigger. */
const SHORT_LABELS = new Map(
  PERMISSION_MODES.flatMap((mode) => (mode.short ? [[mode.value, mode.short] as const] : [])),
);

/** The composer's mode options, from the one permission-mode vocabulary. */
export const MODE_OPTIONS: ModeOption[] = PERMISSION_MODE_OPTIONS.map((mode) => {
  const short = SHORT_LABELS.get(mode.value);
  return {
    value: mode.value,
    label: mode.label,
    description: mode.description,
    ...(short ? { short } : {}),
  };
});

/** The plan surfaces speak in mode choices; each permission mode is one. */
export const MODE_CHOICES: Record<string, PlanModeChoice> = Object.fromEntries(
  PERMISSION_MODE_OPTIONS.map((mode) => [
    mode.value,
    { mode: mode.value, label: mode.label, description: mode.description },
  ]),
);

export { PLAN_REJECT_ANSWER };

/** A plan-approval prompt's accept options as mode choices, with the wire's
 *  verbatim labels kept for the answer. The daemon keys these options off
 *  stable keywords; `planAcceptedMode` is the one shared reading of them. */
export function planChoicesOf(prompt: QuestionPromptView): {
  options: PlanModeChoice[];
  wireLabelByMode: Record<string, string>;
} {
  const options: PlanModeChoice[] = [];
  const wireLabelByMode: Record<string, string> = {};
  for (const option of prompt.options) {
    const mode = planAcceptedMode(option.label);
    const choice = MODE_CHOICES[mode];
    if (!choice || wireLabelByMode[mode]) continue;
    options.push(choice);
    wireLabelByMode[mode] = option.label;
  }
  return { options, wireLabelByMode };
}

export function planResolutionOf(part: QuestionConversationPart): PlanResolution | null {
  // The recorded answer names the note; a machine's echo carries it after the label.
  const note = (part.note ?? part.answers?.[0]?.[1] ?? "").trim() || undefined;
  if (planRejected(part)) return { kind: "rejected", ...(note ? { note } : {}) };
  if (part.status === "answered") {
    const label = part.answers?.[0]?.[0] ?? "";
    return { kind: "approved", mode: MODE_CHOICES[planAcceptedMode(label)], ...(note ? { note } : {}) };
  }
  return null;
}

/** The card's question, spoken by the shared presentation — the same words
 *  the Slack card heads with. */
export function permissionTitle(part: PermissionConversationPart): string {
  return presentPermission(askFromPart(part)).title;
}

/** What the inset says when the ask carries no subject at all: the tool that
 *  raised it, where the title does not already name it. */
export function askedBy(part: PermissionConversationPart): string | undefined {
  return requestedBy(part);
}

export { EXEC_TITLE, alwaysScope };

/** How the card's inset paints each subject format. The harness's own classes
 *  keep their painting (a shell prompt, a path's trail, a link); the rest read
 *  in the plain mono inset under the class that raised them. */
const KIND_BY_FORMAT: Partial<Record<SubjectFormat, PermissionCardProps["kind"]>> = {
  command: "shell",
  path: "edit",
  url: "network",
};

function insetKind(
  format: SubjectFormat | undefined,
  canonical: PermissionCardProps["kind"],
): PermissionCardProps["kind"] {
  const painted = format ? KIND_BY_FORMAT[format] : undefined;
  if (painted) return painted;
  return canonical === "shell" || canonical === "edit" || canonical === "network"
    ? "other"
    : canonical;
}

/** The card's props, rendered from the ask's shared presentation — the model
 *  the Slack card renders too, so what the two surfaces say about an ask is
 *  one decision. The wire offers flat options; the card's always-allow
 *  disclosure takes the `allow_always` one at project scope.
 *
 *  `approval` is the shell's answer to "could approving this reach the agent?".
 *  Where it cannot, the approving actions are not rendered at all and the card
 *  says — in that stance's own words, which ride with the verdict — what
 *  refused them; declining stays, because that is what releases the turn.
 *
 *  `call` is the tool call the ask gates, when the transcript holds it: an ask
 *  that names nothing else shows the call's input.
 *
 *  `canAnswerAlways` is the server's word on whether this reader may give a
 *  standing answer (the chat's own owner). Without it the card offers allow
 *  and decline only. */
export function permissionCardProps(
  part: PermissionConversationPart,
  queue: { position: number; of: number } | undefined,
  onDecide: (optionId: string) => void,
  onOpenUrl: (url: string) => void,
  approval: ApprovalVerdict = APPROVAL_REACHES,
  call?: AskCall,
  canAnswerAlways = true,
  onOpenFile?: (path: string) => void,
): PermissionCardProps {
  const shown = presentPermission(askFromPart(part, call));
  const decisions = shown.decisions;
  const byId = new Map(decisions.map((decision) => [decision.optionId, decision]));
  const allow = byId.get("allow_once") ?? decisions[0];
  const deny = byId.get("reject_once") ?? byId.get("cancelled") ?? decisions[decisions.length - 1];
  const always = approval.allowed && canAnswerAlways ? byId.get("allow_always") : undefined;
  const language = shown.subject?.language;
  return {
    title: shown.title,
    pattern: shown.subject?.text ?? "",
    missingSubject: shown.missingSubject,
    waiting: shown.waiting,
    kind: insetKind(shown.subject?.format, part.canonicalKind),
    language: language === "sql" || language === "python" ? language : undefined,
    register: shown.subject?.format === "sentence" ? "sentence" : "code",
    note: shown.note,
    preview: shown.change,
    facts: shown.facts,
    details: shown.details,
    notebook: shown.notebook,
    onOpenFile: shown.notebook ? onOpenFile : undefined,
    queue,
    allow: approval.allowed ? { optionId: allow.optionId, label: allow.label } : undefined,
    refusal: approval.allowed ? undefined : approval.refusal,
    deny: { optionId: deny.optionId, label: deny.label },
    always: always
      ? {
          label: always.label,
          options: [{ optionId: always.optionId, label: always.label, scope: "project" }],
        }
      : undefined,
    alwaysScope: always ? shown.alwaysScope : undefined,
    onDecide,
    onOpenUrl,
  };
}

/** The meter reads effort by meaning; the no-reasoning floor fills no bars. */
const EFFORT_BARS: Record<string, 0 | 1 | 2 | 3> = {
  none: 0,
  off: 0,
  minimal: 0,
  low: 1,
  medium: 2,
  high: 3,
  xhigh: 3,
  max: 3,
};

export function effortOptions(values: string[]): EffortOption[] {
  return values.map((value, index) => ({
    value,
    label: effortLabel(value),
    bars: EFFORT_BARS[value.toLowerCase()] ?? (Math.min(index, 3) as 0 | 1 | 2 | 3),
  }));
}

export function slashOptions(commands: ComposerCommand[]): SlashCommand[] {
  return commands
    .filter((command) => !command.hidden)
    .map((command) => ({
      command: command.label,
      summary: command.description ?? "",
      usage: command.usage,
    }));
}
