// Model → provider-mark resolution for the composer's model picker, on the shared
// brand registry (ProviderLogo). The registry matches
// provider tokens in the model id or label (`claude`/`anthropic`, `gpt`/`openai`),
// so new model revisions resolve without an edit; anything unrecognized falls back
// to a brain glyph so the picker never renders an empty icon slot.

import { IconBrain } from "@tabler/icons-react";
import type { ReactNode } from "react";

import { resolveProviderMark } from "./ProviderLogo";

const MARK_BOX = 16;

/** Resolve a model's provider mark from its id + label. Falls back to a brain
 *  glyph for anything unmapped (the live model set is Claude + GPT only, so the
 *  fallback is a safety net, not an expected path). */
export function resolveModelMark(modelId: string, label?: string): ReactNode {
  return (
    resolveProviderMark(`${modelId} ${label ?? ""}`) ?? <IconBrain size={MARK_BOX} aria-hidden="true" />
  );
}
