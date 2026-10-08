// The server decides, the UI renders: controls follow the `can_*` /
// `allowed_actions` fields of the policy that enforces the write, so no portal or
// shared-UI source compares a role word or an owner id. Generalizes the Files rung
// scan in packages/api-core/tests/files/authz/test_files_authz_hygiene.py to the
// team and platform vocabularies and to `owner_user_id` comparisons.

import { describe, expect, it } from "vitest";

import { EXTRA_ALLOWLIST, countPerFile, ownerIdComparisons, productSources, roleBranches } from "./scan";

const SCANNED = [...productSources("apps/web/src"), ...productSources("packages/ui/src")];

// Today's branches. A file may only lose entries; fixing one means lowering its
// count here. Some hits are the same word in another vocabulary (a sort key, a
// connection setting's owner) and stay listed until the code stops spelling it.
const ROLE_BRANCHES_ALLOWED: Record<string, number> = {
  "apps/web/src/app/guards/RequirePlatformAdmin.tsx": 1,
  // The nav gates moved out of AppLayout into their own hook; same one check.
  "apps/web/src/app/useNavGates.ts": 1,
  "apps/web/src/pages/organization/teams/TeamsPage.tsx": 1,
  "apps/web/src/pages/organization/teams/data/adapt.ts": 1,
  "apps/web/src/pages/organization/teams/data/model.ts": 2,
  "apps/web/src/pages/organization/teams/detail/actions.tsx": 3,
  "apps/web/src/pages/organization/teams/overlays/modals.tsx": 2,
  "apps/web/src/pages/platform/admin/orgs/AdminOrgDetailPage.tsx": 4,
  "apps/web/src/pages/platform/admin/users/AdminUserDetailPage.tsx": 1,
  "packages/ui/src/connections/helpers.ts": 5,
  "packages/ui/src/connections/status.ts": 1,
  ...EXTRA_ALLOWLIST.roleBranches,
};

const OWNER_ID_COMPARISONS_ALLOWED: Record<string, number> = {
  "apps/web/src/pages/workspace/chat/ChatPage.tsx": 1,
  "apps/web/src/pages/workspace/chat/useChatCopyAction.tsx": 1,
  // Which rail section a workspace or chat is filed under (the reader's own or
  // shared with them). One helper; no control is drawn from it.
  "apps/web/src/pages/workspace/workspaces/railGroups.ts": 1,
  ...EXTRA_ALLOWLIST.ownerIdComparisons,
};

describe("role branch scanner", () => {
  it("finds an equality on either side and a case arm, across every vocabulary", () => {
    // The Files rung is spliced in so this file does not trip the Python Files
    // scan (test_files_authz_hygiene.py), which reads the test sources too.
    const source = [
      `if (role === "${"writer"}") return;`,
      'if ("admin" !== team.role) return;',
      "switch (me.platform_role) {",
      '  case "alkera_admin":',
      "}",
    ].join("\n");
    expect(roleBranches(source)).toEqual(["writer", "admin", "alkera_admin"]);
  });

  it("leaves a role passed through to a label alone", () => {
    const source = 'const LABELS = { writer: "Editor" };\nexport const Row = ({ role }) => role;';
    expect(roleBranches(source)).toEqual([]);
  });

  it("finds an owner id compared in the browser, on either side", () => {
    expect(ownerIdComparisons("const mine = chat.owner_user_id === me.id;")).toBe(1);
    expect(ownerIdComparisons("const mine = me.id !== chat.data?.owner_user_id;")).toBe(1);
    expect(ownerIdComparisons("const owner = chat.owner_user_id;")).toBe(0);
  });
});

describe("no role branch in the portal or the shared UI", () => {
  it("role words: only today's files, at today's counts", () => {
    expect(countPerFile(SCANNED, (source) => roleBranches(source).length)).toEqual(ROLE_BRANCHES_ALLOWED);
  });

  it("owner id comparisons: only today's files, at today's counts", () => {
    expect(countPerFile(SCANNED, ownerIdComparisons)).toEqual(OWNER_ID_COMPARISONS_ALLOWED);
  });
});
