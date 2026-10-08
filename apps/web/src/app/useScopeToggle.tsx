// The org/personal scope switch an org admin gets on a role-branched page
// (Usage, Plugins). Each page keeps its own storage key so the choice sticks
// per page; admins land on the org view. A browser that will not remember it
// costs the reader the stickiness and never the page — see @alkera/ui/storage.

import { useState } from "react";

import { SegmentedControl } from "@alkera/ui";
import { safeLocalStorage } from "@alkera/ui/storage";

const SCOPES = [
  { key: "org", label: "Organization" },
  { key: "personal", label: "Personal" },
] as const;

export function useScopeToggle(storageKey: string) {
  const [scope, setScope] = useState<string>(
    () => safeLocalStorage().get(storageKey) ?? "org",
  );
  const pick = (key: string) => {
    setScope(key);
    safeLocalStorage().set(storageKey, key);
  };
  const control = (
    <SegmentedControl options={SCOPES} value={scope} onChange={pick} label="Scope" size="md" />
  );
  return { scope, control };
}
