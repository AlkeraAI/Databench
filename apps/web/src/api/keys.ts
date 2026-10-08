// The one place a portal query key is spelled (the browser-side twin of the
// webview's chatKeys rule).
//
// One key per read. A second spelling of the same key splits the cache: two
// surfaces then hold two copies of one server truth and every writer has to
// remember to refresh both. Hoisting the spellings here also gives anything that
// needs to name a cache slot from OUTSIDE a hook (a server-event map, a mutation's
// `meta.invalidates`, a test seeding state) a stable reference instead of a copied
// literal that silently drifts.
//
// Every value is a plain readonly tuple, React-free, and byte-identical to the
// spelling the owning api module used before it moved here. Parameterised keys
// are functions so the argument rides the key verbatim (including the `undefined`
// a not-yet-routed page passes: that keeps a waiting page on its own entry rather
// than colliding with a real one). The bare prefixes (`all`, `*All`) exist so a
// caller can refresh a whole family with one prefix match.

import type { QueryKey } from "@tanstack/react-query";

/** The listing arguments a Files read carries, already serialised to the wire
 *  spelling the API expects (`orderBy`, `kind`, `starred`, `modifiedAfter`, …).
 *  One flat record of primitives so it may ride a query key: react-query hashes
 *  it with sorted properties, so two equal filter sets share one cache entry
 *  however the caller ordered them. */
export type FilesListParams = Readonly<Record<string, string | undefined>>;

export const keys = {
  auth: {
    me: ["auth", "me"] as const,
    identities: ["auth", "identities"] as const,
    sessions: ["auth", "sessions"] as const,
    mfaStatus: ["auth", "mfa-status"] as const,
    /** The caller's own org memberships: the orgs this browser can switch into. */
    memberships: ["auth", "memberships"] as const,
    /** The single sign-on link request parked for this browser (`/link-sso`). */
    ssoLink: ["auth", "sso-link"] as const,
    deviceInfo: (userCode: string) => ["auth", "device-info", userCode] as const,
  },
  /** The person's own export and deletion requests (`/api/v1/me/account`). */
  account: {
    deletionPlan: ["account", "deletion-plan"] as const,
    deletion: ["account", "deletion"] as const,
    exports: ["account", "exports"] as const,
  },
  oauth: {
    providers: ["oauth", "providers"] as const,
    registerContext: (ticket: string | undefined) => ["oauth", "register-context", ticket] as const,
  },
  invitations: {
    all: ["invitations"] as const,
    byToken: (token: string | undefined) => ["invitations", "by-token", token] as const,
    team: (teamId: string | undefined) => ["invitations", "team", teamId] as const,
    me: ["invitations", "me"] as const,
  },
  me: {
    credits: ["me", "credits"] as const,
    usage: (window: string) => ["me", "usage", window] as const,
    /** What the caller has stored, and the tightest ceiling that applies to them.
     *  Two surfaces that know nothing of each other refresh it: the dashboard and
     *  the Usage page read it, and an admin writing a member limit invalidates it. */
    storage: ["me", "storage"] as const,
    /** The caller's own allowances, summed over their teams. */
    allocations: ["me", "allocations"] as const,
    /** The caller's own preference document, read whole. */
    preferences: ["me", "preferences"] as const,
    /** The gateway models this caller may start a chat on. */
    chatModels: ["me", "chat-models"] as const,
  },
  dashboard: {
    identity: ["dashboard", "identity"] as const,
  },
  chats: {
    all: ["chats"] as const,
    one: (chatId: string | undefined) => ["chats", chatId] as const,
    messages: (chatId: string | undefined) => ["chats", chatId, "messages"] as const,
    /** The Files nodes linked to this chat, as the CALLER can read them: the
     *  list is a per-request decision, so it is keyed by the chat and never
     *  shared with another reader's answer. */
    attachments: (chatId: string | undefined) => ["chats", chatId, "attachments"] as const,
  },
  /** The workspaces this reader may open (their own and the ones shared with
   *  them), and one of them. Under one prefix so a create, a rename or a
   *  delete refreshes the list and the open workspace together. */
  workspaces: {
    all: ["workspaces"] as const,
    one: (workspaceId: string | undefined) => ["workspaces", workspaceId] as const,
  },
  /** The reader's own main workspace, made on first ask. Its own family: the
   *  workspace list stops listing a main workspace that holds only warmed
   *  spares, and under the list's prefix this read would be repeated on every
   *  change to any workspace for an answer that does not move. */
  workspaceMain: {
    mine: ["workspace-main"] as const,
  },
  /** The tabs this reader left open beside a chat, and which was in front.
   *
   *  Its own family rather than a slot under `chats`, because nothing that
   *  changes a chat changes this reader's layout of it — and the chat family is
   *  invalidated as a PREFIX many times a minute by the event stream, which on
   *  an idle chat page made this read the third-busiest request on the wire for
   *  an answer that had not moved. It is written by one mutation and seeded from
   *  that mutation's own reply.
   *
   *  The trade that buys: there is no server event for a layout change, so two
   *  tabs of the same reader converge only when one of them is focused again,
   *  not while both sit open. That is last-writer-wins, on purpose — a layout is
   *  a view of a chat and not a fact about it, and the alternative was re-asking
   *  for it every time anything in the chat family moved. */
  chatWorkspace: {
    all: ["chat-workspace"] as const,
    one: (chatId: string | undefined) => ["chat-workspace", chatId] as const,
  },
  objects: {
    all: ["objects"] as const,
    /** One kind of object, listed. Under the family prefix, so a promote or a
     *  save refreshes it with everything else. */
    ofType: (type: string) => ["objects", "of-type", type] as const,
    one: (objectId: string | undefined) => ["objects", objectId] as const,
    rows: (objectId: string | undefined, offset: number, limit: number) =>
      ["objects", objectId, "rows", offset, limit] as const,
    /** Every row a chart draws, read as one entry: the table pages, a chart
     *  does not — a 90-day series drawn from one page is a 50-day series with
     *  nothing on screen to say so. */
    chartRows: (objectId: string | undefined) => ["objects", objectId, "chart-rows"] as const,
  },
  files: {
    all: ["files"] as const,
    /** The caller's one org drive. */
    drive: ["files", "drive"] as const,
    item: (itemId: string | undefined) => ["files", "item", itemId] as const,
    /** Every item the browser holds, for a change (a role) that moves what
     *  each of them says the reader may do. */
    itemAll: ["files", "item"] as const,
    /** A folder's listing. The drive, the serialised filters + order and the page
     *  size all ride the key because each changes WHICH rows the server returns:
     *  a filtered view sharing the unfiltered entry would show the wrong page
     *  while the refetch is in flight, and two readers of one folder asking for
     *  different page sizes would share whichever page mounted first. */
    childrenAll: ["files", "children"] as const,
    /** ONE folder's listings, whatever filters or page size they asked for — the
     *  prefix between the whole family and a single entry. A server event that
     *  names the parent it changed refreshes this instead of every folder the
     *  browser holds, which is what keeps a chat folder written to many times a
     *  second from re-fetching the rest of the drive with it. */
    childrenOf: (driveId: string | undefined, parentId: string | undefined) =>
      ["files", "children", driveId, parentId] as const,
    children: (
      driveId: string | undefined,
      parentId: string | undefined,
      params: FilesListParams,
      limit: number,
    ) => ["files", "children", driveId, parentId, params, limit] as const,
    /** A drive-wide search. The query text rides in `params` with the filters,
     *  so a keystroke starts a new entry and react-query cancels the old one. */
    searchAll: ["files", "search"] as const,
    search: (driveId: string | undefined, params: FilesListParams) =>
      ["files", "search", driveId, params] as const,
    recent: ["files", "recent"] as const,
    starred: ["files", "starred"] as const,
    sharedWithMe: ["files", "shared-with-me"] as const,
    trashAll: ["files", "trash"] as const,
    trash: (driveId: string | undefined) => ["files", "trash", driveId] as const,
    operation: (operationId: string | undefined) => ["files", "operation", operationId] as const,
    leasesAll: ["files", "leases"] as const,
    leases: (driveId: string | undefined) => ["files", "leases", driveId] as const,
    versions: (nodeId: string | undefined) => ["files", "versions", nodeId] as const,
    /** Who can reach a node. `effective` rides the key because it changes WHICH
     *  rows come back — the direct grants carry the share id a revoke needs, the
     *  effective set carries the ancestor a grant descended from and no id at
     *  all, and a share dialog reads both at once. */
    permissionsAll: ["files", "permissions"] as const,
    permissions: (nodeId: string | undefined, effective = false) =>
      ["files", "permissions", nodeId, effective] as const,
    /** Who a share of this node may name, matching what the reader typed. Keyed
     *  by the node because the read is decided as a share OF it. */
    shareCandidates: (nodeId: string | undefined, query: string) =>
      ["files", "shareCandidates", nodeId, query] as const,
    activity: (nodeId: string | undefined) => ["files", "activity", nodeId] as const,
    /** The named folders a drive keeps for a person — their home, their chats,
     *  their chat templates. Keyed by the drive because the answer is the
     *  CALLER's own set of them, and a drive is the only argument the read
     *  takes. */
    places: (driveId: string | undefined) => ["files", "places", driveId] as const,
  },
  chatTemplates: {
    all: ["chat-templates"] as const,
    one: (templateId: string | undefined) => ["chat-templates", templateId] as const,
  },
  machines: {
    /** The org's workspace machine, as the chat banner reads it. */
    current: ["machines", "current"] as const,
    /** Where a chat of one workspace would run (a pinned workspace runs on its machine). */
    currentFor: (workspaceId: string | undefined) => ["machines", "current", workspaceId] as const,
    /** The org's machines: the list, and each machine's page under it. */
    org: ["machines", "org"] as const,
    orgOne: (machineId: string | undefined) => ["machines", "org", machineId] as const,
    /** What the org may buy. */
    offerings: ["machines", "offerings"] as const,
    /** What buying one offering at one disk size would cost, and whether it would be admitted. */
    quote: (offeringId: string | undefined, storageGb: number) => ["machines", "quote", offeringId, storageGb] as const,
    /** What one machine would cost with its disk grown to a size, and whether the grow would be admitted. */
    diskQuote: (machineId: string, volumeGb: number) => ["machines", "diskQuote", machineId, volumeGb] as const,
    /** Whether the org may buy another machine now. */
    buying: ["machines", "buying"] as const,
    /** The org's compute settings, its default machine for new workspaces among them. */
    computeSettings: ["machines", "settings"] as const,
    /** Every workspace's machine, and one workspace's. */
    workspaceAll: ["machines", "workspace"] as const,
    workspace: (workspaceId: string | undefined) => ["machines", "workspace", workspaceId] as const,
    /** Machine spend per machine per day over a window, optionally one team's. */
    usage: (since: string, until: string, teamId: string | undefined) =>
      ["machines", "usage", since, until, teamId] as const,
  },
  kb: {
    all: ["kb"] as const,
    browse: ["kb", "browse"] as const,
    catalog: ["kb", "browse", "catalog"] as const,
    attached: (lineageUrn: string) => ["kb", "browse", "attached", lineageUrn] as const,
    repos: ["kb", "repos"] as const,
    scopes: ["kb", "scopes"] as const,
  },
  gate: {
    all: ["gate"] as const,
    status: ["gate", "status"] as const,
    runsAll: ["gate", "runs"] as const,
    summary: ["gate", "summary"] as const,
    runs: (repo?: string) => ["gate", "runs", repo ?? ""] as const,
    run: (runId: string | undefined) => ["gate", "run", runId] as const,
    driftAll: ["gate", "drift"] as const,
    drift: (repo?: string) => ["gate", "drift", repo ?? ""] as const,
    activityAll: ["gate", "activity"] as const,
    activity: (repo?: string) => ["gate", "activity", repo ?? ""] as const,
    waivers: ["gate", "waivers"] as const,
    tokens: ["gate", "tokens"] as const,
    githubInstallations: ["gate", "github", "installations"] as const,
  },
  billing: {
    summary: ["billing", "summary"] as const,
  },
  org: {
    billing: ["org", "billing"] as const,
    /** The org admin's storage dashboard — the org ceiling plus every member's
     *  usage and limits, read as one document. */
    storage: ["org", "storage"] as const,
    settings: ["org", "settings"] as const,
    slack: ["org", "slack"] as const,
    /** What Slack itself says about the install: the bot's handle and whether a reinstall is due. */
    slackInstallHealth: ["org", "slack", "health"] as const,
    syncSettings: ["org", "sync-settings"] as const,
    usage: (window: string, teamId?: string) =>
      ["org", "usage", window, teamId ?? ""] as const,
  },
  orgAdmin: {
    members: ["org-admin", "members"] as const,
    /** The audit page's filters object rides the key as a plain record of
     *  primitives; react-query hashes it with sorted keys, so two equal filter
     *  sets share one entry however their properties were ordered. */
    audit: (offset: number, limit: number, filters: Readonly<Record<string, string | undefined>>) =>
      ["org-admin", "audit", offset, limit, filters] as const,
    sso: ["org-admin", "sso"] as const,
    modelProviders: ["org-admin", "model-providers"] as const,
    deploymentHealth: ["org-admin", "deployment-health"] as const,
  },
  teams: {
    all: ["teams"] as const,
    membersAll: ["members"] as const,
    members: (teamId: string | undefined, includeDescendants: boolean) =>
      ["members", teamId, includeDescendants] as const,
    memberPlanAll: ["member-plan"] as const,
    memberPlan: (teamId: string | undefined, userId: string | undefined) =>
      ["member-plan", teamId, userId] as const,
    /** A team's allocation table — its own limits, its sub-teams', its members'. */
    allocationsAll: ["team-allocations"] as const,
    allocations: (teamId: string) => ["team-allocations", teamId] as const,
  },
  connections: {
    /** Everything the signed-in person can use — their own rows and their teams'.
     *  Separate from `teamConnections.team`, which is one team's admin list. */
    me: ["connections", "me"] as const,
  },
  teamConnections: {
    all: ["team-connections"] as const,
    team: (teamId: string) => ["team-connections", teamId] as const,
    forms: ["connection-forms"] as const,
    verification: (verificationId: string | null) => ["connection-verification", verificationId] as const,
  },
  connectionInventory: {
    me: ["connection-inventory", "me"] as const,
    org: ["connection-inventory", "org"] as const,
  },
  publicConfig: ["public-config"] as const,
  lineage: {
    graph: ["lineage", "graph"] as const,
  },
  /** A notebook: its live document view and kernel, by drive and item. */
  notebooks: {
    all: ["notebooks"] as const,
    view: (driveId: string, itemId: string) => ["notebooks", "view", driveId, itemId] as const,
    /** The notebook as the drive stores it: cells and saved outputs. */
    stored: (driveId: string, itemId: string, version: string) => ["notebooks", "stored", driveId, itemId, version] as const,
    envs: (driveId: string, itemId: string) => ["notebooks", "envs", driveId, itemId] as const,
    /** The chat whose pane opens a notebook, or a new one in a folder. */
    editor: (driveId: string, itemId: string) => ["notebooks", "editor", driveId, itemId] as const,
    /** An output a notebook stores beside itself, as a chat card names it:
     *  by the notebook's path in the chat and the output's hash. */
    storedOutput: (chatId: string, path: string, sha256: string) => ["notebooks", "output", chatId, path, sha256] as const,
    /** The connections the notebook's SQL cells may name. */
    connections: (driveId: string, itemId: string) => ["notebooks", "connections", driveId, itemId] as const,
    packages: (driveId: string, itemId: string, envId: string) => ["notebooks", "packages", driveId, itemId, envId] as const,
    /** Every environment's packages for one notebook, for a refetch. */
    packagesOf: (driveId: string, itemId: string) => ["notebooks", "packages", driveId, itemId] as const,
  },
  admin: {
    /** One person's export and deletion requests, as the support tool reads them. */
    userAccount: (userId: string | undefined) => ["admin", "users", userId, "account"] as const,
    /** One person's exports, as the support tool reads them. */
    userExports: (userId: string | undefined) => ["admin", "users", userId, "account", "exports"] as const,
    orgs: ["admin", "orgs"] as const,
    org: (orgId: string | undefined) => ["admin", "orgs", orgId] as const,
    orgTeams: (orgId: string | undefined) => ["admin", "orgs", orgId, "teams"] as const,
    orgMembers: (orgId: string | undefined) => ["admin", "orgs", orgId, "members"] as const,
    orgSettings: (orgId: string | undefined) => ["admin", "orgs", orgId, "settings"] as const,
    orgStorage: (orgId: string | undefined) => ["admin", "orgs", orgId, "storage"] as const,
    orgLiveEditing: (orgId: string | undefined) => ["admin", "orgs", orgId, "live-editing"] as const,
    orgChats: (orgId: string | undefined) => ["admin", "orgs", orgId, "chats"] as const,
    orgErrors: (orgId: string | undefined) => ["admin", "orgs", orgId, "errors"] as const,
    orgAudit: (orgId: string | undefined) => ["admin", "orgs", orgId, "audit"] as const,
    opsSummary: ["admin", "ops", "summary"] as const,
    users: ["admin", "users"] as const,
    userIpInfo: (userId: string | undefined) => ["admin", "users", userId, "ip-info"] as const,
    overview: (window: string) => ["admin", "overview", window] as const,
    billingUser: (userId: string | undefined) => ["admin", "billing", "user", userId] as const,
    /** What resetting one user's usage would refund and erase — read when the
     *  confirmation opens, under the user's billing prefix so it refreshes with it. */
    usageResetPreview: (userId: string | undefined) =>
      ["admin", "billing", "user", userId, "usage-reset"] as const,
    usageUser: (userId: string | undefined, window: string) =>
      ["admin", "usage", "user", userId, window] as const,
    billingOrg: (orgId: string | undefined) => ["admin", "billing", "org", orgId] as const,
    usageOrg: (orgId: string | undefined, window: string) =>
      ["admin", "usage", "org", orgId, window] as const,
    recurringGrants: ["admin", "recurring-grants"] as const,
    recurringGrantsOrg: (orgId: string | undefined) =>
      ["admin", "recurring-grants", "org", orgId] as const,
    catalogModels: ["admin", "catalog", "models"] as const,
    enterpriseOrgs: ["admin", "enterprise-orgs"] as const,
    enterpriseOrg: (orgId: string | null) => ["admin", "enterprise-orgs", orgId] as const,
    enterpriseOrgUsage: (orgId: string | null, periods: number) =>
      ["admin", "enterprise-orgs", orgId, "usage", periods] as const,
    entitlements: (orgId: string | null) => ["admin", "entitlements", orgId] as const,
    crashReports: (unread: boolean) => ["admin", "crash-reports", unread] as const,
    crashReport: (reportId: string | null) =>
      ["admin", "crash-reports", "detail", reportId] as const,
    /** The filters object rides the key as a plain record of primitives (see `orgAdmin.audit`). */
    auditLogs: (
      page: number,
      pageSize: number,
      filters: Readonly<Record<string, string | undefined>> = {},
    ) => ["admin", "audit-logs", page, pageSize, filters] as const,
    platformCap: ["admin", "platform-cap"] as const,
    computeOrg: (orgId: string | undefined) => ["admin", "compute", "org", orgId] as const,
    slackOrg: (orgId: string | undefined) => ["admin", "slack", "org", orgId] as const,
    machines: ["admin", "machines"] as const,
    adminMachinesFleet: (includeGone: boolean) => ["admin", "machines", "fleet", includeGone] as const,
    adminMachinesUnmanaged: ["admin", "machines", "unmanaged"] as const,
    adminMachinesTypes: ["admin", "machines", "types"] as const,
    adminMachinesDetail: (machineId: string | undefined) =>
      ["admin", "machines", "detail", machineId] as const,
    orgDedicatedCompute: (orgId: string | undefined) =>
      ["admin", "orgs", orgId, "dedicated-compute"] as const,
    userBans: ["admin", "bans", "users"] as const,
    domainBans: ["admin", "bans", "domains"] as const,
    /** The machine catalog: every offering, retired ones included. */
    computeOfferings: ["admin", "compute-offerings"] as const,
    /** The machines one org holds, bought or given. */
    orgMachines: (orgId: string | undefined) => ["admin", "orgs", orgId, "machines"] as const,
  },
} as const;

/** Every key that needs no argument, in declaration order — the census a test
 *  (or a "does this key exist?" guard) walks. Parameterised keys are functions
 *  and are skipped; they can only be enumerated with sample arguments. */
export function allStaticKeys(): QueryKey[] {
  const out: QueryKey[] = [];
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) {
      out.push(node as QueryKey);
      return;
    }
    if (typeof node === "function" || node === null || typeof node !== "object") return;
    for (const value of Object.values(node as Record<string, unknown>)) walk(value);
  };
  walk(keys);
  return out;
}
