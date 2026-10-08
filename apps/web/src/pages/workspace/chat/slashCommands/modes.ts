// The editor must resolve a mode word to the canonical PermissionMode the daemon
// accepts (`harness.set_permission_mode` validates against the literal — it does
// NOT normalize aliases). This mirrors `MODE_ALIASES` in
// `apps/cli/alkera_cli/harness/permission_mode.py`; keep the two in lockstep.
export const MODE_ALIASES: Record<string, string> = {
  read_only: "read_only",
  "read-only": "read_only",
  readonly: "read_only",
  ro: "read_only",
  default: "default",
  normal: "default",
  "accept-edits": "default",
  accept_edits: "default",
  auto: "auto",
  automatic: "auto",
  plan: "plan",
  bypass: "bypass",
  yolo: "bypass",
};

/** Resolve a typed/shortcut mode word to its canonical mode, or undefined. */
export function resolveMode(word: string): string | undefined {
  return MODE_ALIASES[word.trim().toLowerCase()];
}
