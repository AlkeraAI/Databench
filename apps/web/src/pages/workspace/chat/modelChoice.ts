import { middleEffort } from "@alkera/chat-model";

/** The catalog fields that determine one composer's model and effort. */
export interface ChoiceModel {
  id: string;
  efforts: string[];
  /** `undefined` means the catalog has not resolved this model yet. */
  defaultEffort: string | null | undefined;
}

interface ChoicePair {
  model?: string | null;
  effort?: string | null;
}

export interface ModelChoice {
  model: string;
  efforts: string[];
  effort: string | undefined;
  reasoningVisible: boolean;
}

/** Whether a model explicitly offers an effort value. */
function offers(efforts: readonly string[], effort: string | null | undefined): effort is string {
  return typeof effort === "string" && efforts.includes(effort);
}

/** Resolve a model/effort pair without carrying one model's effort into another.
 *  An unresolved catalog fails closed so reasoning cannot flash before a
 *  selected model's default is known. */
export function resolveModelChoice(
  models: readonly ChoiceModel[],
  seed: ChoicePair | undefined,
  override: ChoicePair | undefined,
): ModelChoice {
  const selected =
    models.find((model) => model.id === override?.model) ??
    models.find((model) => model.id === seed?.model) ??
    models[0];
  if (!selected) {
    return { model: "", efforts: [], effort: undefined, reasoningVisible: false };
  }

  let effort: string | undefined;
  if (selected.defaultEffort !== undefined) {
    effort = offers(selected.efforts, selected.defaultEffort)
      ? selected.defaultEffort
      : middleEffort(selected.efforts);
  }
  if (seed?.model === selected.id && offers(selected.efforts, seed.effort)) effort = seed.effort;
  if (override?.model === selected.id && offers(selected.efforts, override.effort)) {
    effort = override.effort;
  }

  return {
    model: selected.id,
    efforts: selected.efforts,
    effort,
    reasoningVisible: effort !== undefined && effort !== "none",
  };
}

/** Whether the transcript shows the model's reasoning.
 *
 *  A shell that drives its own harness picks an effort, and "none" means the
 *  reader asked not to see thinking — an unresolved catalogue fails closed so
 *  reasoning cannot flash before the selected model's default is known.
 *
 *  A shell with NO harness of its own — the browser, reading a transcript a
 *  workspace machine published — has no effort to choose and no catalogue to
 *  resolve, so that fail-closed rule silently deleted every reasoning part the
 *  machine had already written. There is nothing to fail closed about: what the
 *  machine published is the record, and the transcript renders it the way it
 *  renders it everywhere else — its own collapsed, labelled block, never as the
 *  answer.
 */
export function reasoningIsVisible(
  choice: ModelChoice,
  caps: { opencodeActive: boolean; modelCatalog?: boolean },
): boolean {
  return caps.modelCatalog ?? caps.opencodeActive ? choice.reasoningVisible : true;
}

