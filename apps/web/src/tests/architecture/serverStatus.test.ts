// The server decides, the UI renders: a status pill draws the StatusFact the
// server wrote (state, label, tone, sentence), so no portal, shared-UI or
// notebook source turns a server status word into copy, a tone or a control.
// This counts every place one does today: an equality between a status field
// and a word, a `switch` over one, and a lookup table keyed by a status type.
//
// The allowlist is today's sites and may only shrink. Some are client-owned
// state that shares a field name (an upload row, a tool part, a composer
// chip); they stay listed until that state is renamed or moves to the server.
// A surface moved onto StatusFact lowers its count here, and a new mapper
// anywhere fails.

import { describe, expect, it } from "vitest";

import { countPerFile, EXTRA_ALLOWLIST, productSources, statusBranches } from "./scan";

const SCANNED = [
  ...productSources("apps/web/src"),
  ...productSources("packages/ui/src"),
  ...productSources("packages/notebook-ui/src"),
];

const STATUS_BRANCHES_ALLOWED: Record<string, number> = {
  ...EXTRA_ALLOWLIST.statusBranches,
  "apps/web/src/api/events/RealtimeBridge.tsx": 1,
  "apps/web/src/api/events/sseClient.ts": 1,
  "apps/web/src/api/events/status.ts": 1,
  "apps/web/src/api/filesUpload.ts": 6,
  "apps/web/src/api/orgs.ts": 1,
  "apps/web/src/api/realtime/crdt/channel.ts": 2,
  "apps/web/src/api/realtime/publisherLinks.ts": 1,
  "apps/web/src/api/realtime/wsClient.ts": 2,
  "apps/web/src/api/teamConnections.ts": 2,
  "apps/web/src/app/boot/RootErrorBoundary.tsx": 4,
  "apps/web/src/pages/auth/ChooseOrgPage.tsx": 1,
  "apps/web/src/pages/auth/EmailVerificationGatePage.tsx": 2,
  "apps/web/src/pages/auth/VerifyEmailPage.tsx": 1,
  "apps/web/src/pages/organization/machines/MachineDetailPage.tsx": 8,
  "apps/web/src/pages/organization/machines/MachinesPage.tsx": 1,
  "apps/web/src/pages/organization/org/AuditLogPage.tsx": 3,
  "apps/web/src/pages/organization/org/SsoSettingsPage.tsx": 3,
  "apps/web/src/pages/organization/teams/TeamsPage.tsx": 7,
  "apps/web/src/pages/organization/teams/detail/TeamDetail.tsx": 6,
  "apps/web/src/pages/platform/admin/ops/AdminOpsPage.tsx": 8,
  "apps/web/src/pages/platform/admin/orgs/AdminOrgDetailPage.tsx": 2,
  "apps/web/src/pages/platform/admin/users/AccountRequestsCard.tsx": 1,
  "apps/web/src/pages/workspace/chat/ChatPage.tsx": 1,
  "apps/web/src/pages/workspace/chat/ChatRuntimeLayout.tsx": 6,
  "apps/web/src/pages/workspace/chat/MachineBanner.tsx": 8,
  "apps/web/src/pages/workspace/chat/PinnedMachineBanner.tsx": 1,
  "apps/web/src/pages/workspace/chat/blobModel.ts": 1,
  "apps/web/src/pages/workspace/chat/chatStore.ts": 1,
  "apps/web/src/pages/workspace/chat/controller/useChatTranscript.ts": 1,
  "apps/web/src/pages/workspace/chat/controller/useComposerPrefs.ts": 5,
  "apps/web/src/pages/workspace/chat/data/CloudDataSource.ts": 4,
  "apps/web/src/pages/workspace/chat/data/chatFiles.ts": 1,
  "apps/web/src/pages/workspace/chat/data/convergence.ts": 1,
  "apps/web/src/pages/workspace/chat/data/errors.ts": 4,
  "apps/web/src/pages/workspace/chat/data/harnessEventFold.ts": 40,
  "apps/web/src/pages/workspace/chat/entries.tsx": 9,
  "apps/web/src/pages/workspace/chat/homeRows.ts": 1,
  "apps/web/src/pages/workspace/chat/options.ts": 1,
  "apps/web/src/pages/workspace/chat/slashCommands/panels.tsx": 4,
  "apps/web/src/pages/workspace/chat/startChatWith.ts": 1,
  "apps/web/src/pages/workspace/chat/stripPermissionParts.ts": 1,
  "apps/web/src/pages/workspace/chat/workspace/FileTab.tsx": 1,
  "apps/web/src/pages/workspace/chat/workspace/FileTabPreview.tsx": 2,
  "apps/web/src/pages/workspace/chat/workspace/FilesStatusBar.tsx": 1,
  "apps/web/src/pages/workspace/chat/workspace/FilesTab.tsx": 1,
  "apps/web/src/pages/workspace/chat/workspace/notebook/NotebookTab.tsx": 1,
  "apps/web/src/pages/workspace/chat/workspace/notebook/newNotebook.ts": 1,
  "apps/web/src/pages/workspace/chat/workspace/notebook/notebookRuntime.ts": 17,
  "apps/web/src/pages/workspace/chat/workspace/notebook/notebookWire.ts": 4,
  "apps/web/src/pages/workspace/connections/ConnectionDialog.tsx": 6,
  "apps/web/src/pages/workspace/dashboard/DashboardPage.tsx": 3,
  "apps/web/src/pages/workspace/dashboard/model.ts": 1,
  "apps/web/src/pages/workspace/files/ShareDialog.tsx": 1,
  "apps/web/src/pages/workspace/files/UploadTray.tsx": 10,
  "apps/web/src/pages/workspace/files/live/LiveBadge.tsx": 1,
  "apps/web/src/pages/workspace/files/live/LiveRowChip.tsx": 2,
  "apps/web/src/pages/workspace/files/live/useFolderLiveness.ts": 3,
  "apps/web/src/pages/workspace/files/liveRoot/liveCopy.ts": 6,
  // The one switch over the server's Files status: the server decides the
  // state and the browser picks which sentence in liveCopy.ts says it (an
  // exception for these sentences, not a precedent).
  "apps/web/src/pages/workspace/files/liveRoot/liveness.ts": 1,
  "apps/web/src/pages/workspace/files/preview/FilePreviewModal.tsx": 1,
  "apps/web/src/pages/workspace/files/preview/usePreviewContent.ts": 1,
  "apps/web/src/pages/workspace/files/state/uploadTray.ts": 14,
  "apps/web/src/pages/workspace/files/useUploads.ts": 6,
  "apps/web/src/pages/workspace/objects/ObjectPage.tsx": 5,
  "apps/web/src/pages/workspace/workspaces/WorkspaceMachine.tsx": 5,
  "apps/web/src/pages/workspace/workspaces/WorkspaceOverview.tsx": 1,
  "apps/web/src/pages/workspace/workspaces/useWorkspaceRail.tsx": 1,
  "packages/notebook-ui/src/components/panels/EnvironmentPanel.tsx": 3,
  "packages/notebook-ui/src/editor/NotebookEditor.tsx": 1,
  "packages/notebook-ui/src/editor/NotebookToolbar.tsx": 6,
  "packages/notebook-ui/src/editor/status.ts": 2,
  "packages/notebook-ui/src/frame/FramedOutput.tsx": 5,
  "packages/ui/src/chat/activity/Activity.tsx": 6,
  "packages/ui/src/chat/composer/Composer.tsx": 8,
  "packages/ui/src/chat/composer/uploads.ts": 1,
  "packages/ui/src/chat/home/ChatRow.tsx": 2,
  "packages/ui/src/chat/panel/ChatPanel.tsx": 4,
  "packages/ui/src/chat/plan/planModel.ts": 1,
  "packages/ui/src/chat/subagent/SubagentBlock.tsx": 3,
  "packages/ui/src/chat/tools/bash.tsx": 1,
  "packages/ui/src/chat/tools/edit.tsx": 1,
  "packages/ui/src/chat/tools/generic.tsx": 5,
  "packages/ui/src/chat/tools/notebook.tsx": 1,
  "packages/ui/src/chat/tools/notebook_show.tsx": 3,
  "packages/ui/src/chat/tools/read.tsx": 1,
  "packages/ui/src/chat/tools/shared.tsx": 3,
  "packages/ui/src/chat/tools/spec.tsx": 3,
  "packages/ui/src/chat/tools/sql_query.tsx": 2,
  "packages/ui/src/chat/tools/step.tsx": 1,
  "packages/ui/src/chat/tools/steps.ts": 5,
  "packages/ui/src/chat/tools/todowrite.tsx": 1,
  "packages/ui/src/chat/tools/webfetch.tsx": 1,
  "packages/ui/src/chat/tools/write.tsx": 2,
  "packages/ui/src/compute/MachineCardView.tsx": 5,
  "packages/ui/src/compute/format.ts": 3,
  "packages/ui/src/preview/PreviewSurface.tsx": 2,
  "packages/ui/src/preview/registry.ts": 1,
  "packages/ui/src/preview/renderers/BinaryFallback.tsx": 4,
  "packages/ui/src/primitives/overlays/Toast/Toast.tsx": 1,
  "packages/ui/src/resources/BlobView/BlobView.tsx": 2,
  "packages/ui/src/resources/ReferenceList/ReferenceList.tsx": 3,
};

describe("status branch scanner", () => {
  it("finds an equality on either side, a switch and a lookup table", () => {
    const source = [
      'if (card.state === "running") return "Running";',
      'const off = "asleep" === chat.machine_status;',
      "switch (move.state) { default: }",
      "const LABEL: Record<KernelState, string> = {};",
      "const COPY: Record<Exclude<ChatMachineStatus, Quiet>, Copy> = {};",
    ].join("\n");
    expect(statusBranches(source)).toHaveLength(5);
  });

  it("leaves a status the server wrote passed through, and an HTTP status", () => {
    const source = [
      "<span data-state={status.state}>{status.label}</span>",
      "if (error.status === 404) return null;",
      "const tone = status.tone;",
    ].join("\n");
    expect(statusBranches(source)).toEqual([]);
  });
});

describe("status words mapped in the browser", () => {
  it("only shrink", () => {
    expect(countPerFile(SCANNED, (source) => statusBranches(source).length)).toEqual(
      STATUS_BRANCHES_ALLOWED,
    );
  });
});
