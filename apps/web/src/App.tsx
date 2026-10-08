import { QueryClientProvider } from "@tanstack/react-query";
import { Suspense, lazy } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { queryClient } from "./api/queryClient";
import { AppLayout } from "./app/AppLayout";
import { ADMIN_HOME, PORTAL_ROUTES, type RouteMount } from "./app/extensions/portal";
import { LegacyParamRedirect, LegacyRedirect } from "./app/LegacyRedirect";
import { DocumentTitle } from "./app/documentTitle";
import { SessionBridge } from "./app/boot/SessionBridge";
import { UnauthorizedBridge } from "./app/boot/UnauthorizedBridge";
import { RealtimeBridge } from "./api/events/RealtimeBridge";
import { OrgDeepLinkGate } from "./app/guards/OrgDeepLinkGate";
import { RequireAuth } from "./app/guards/RequireAuth";
import { RequireOrgAdmin } from "./app/guards/RequireOrgAdmin";
import { RequireSelfHosted } from "./app/guards/RequireSelfHosted";
import { MachinesPage } from "./pages/organization/machines/MachinesPage";
import { MachineDetailPage } from "./pages/organization/machines/MachineDetailPage";
import { RequirePlatformStaff } from "./app/guards/RequirePlatformStaff";
import { RequirePlatformAdmin } from "./app/guards/RequirePlatformAdmin";
import { RealAuthActionsProvider } from "./pages/auth/auth-actions";
import { CompleteProfilePage } from "./pages/auth/CompleteProfilePage";
import { DashboardPage } from "./pages/workspace/dashboard/DashboardPage";
import { LiveDashboardProvider } from "./pages/workspace/dashboard/provider";
import { ChooseOrgPage } from "./pages/auth/ChooseOrgPage";
import { LinkSsoPage } from "./pages/auth/LinkSsoPage";
import { NoOrganizationPage } from "./pages/auth/NoOrganizationPage";
import { DeviceApprovalPage } from "./pages/auth/DeviceApprovalPage";
import { EmailVerificationGatePage } from "./pages/auth/EmailVerificationGatePage";
import { EmailVerificationPage } from "./pages/workspace/EmailVerificationPage";
import { ForgotPasswordPage } from "./pages/auth/ForgotPasswordPage";
import { ChatPage, ChatRoute, ChatStackedRoute, WorkspaceRoute } from "./pages/workspace/chat/ChatPage";
import { ChatRuntimeLayout } from "./pages/workspace/chat/ChatRuntimeLayout";
import { FilesRoute } from "./pages/workspace/files/FilesRoute";
import { BlobSurface } from "./pages/workspace/chat/BlobSurface";
import { BlobsSurface } from "./pages/workspace/chat/BlobsSurface";
import { CompactionSurface } from "./pages/workspace/chat/CompactionSurface";
import { PlanSurface } from "./pages/workspace/chat/PlanSurface";
import { ObjectRoute } from "./pages/workspace/objects/ObjectRoute";
import { ChatTemplatePage } from "./pages/workspace/templates/ChatTemplatePage";
import { LoginPage } from "./pages/auth/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { ResetPasswordPage } from "./pages/auth/ResetPasswordPage";
import { VerifyEmailPage } from "./pages/auth/VerifyEmailPage";
import { OrgSettingsPage, ProfileSettingsPage } from "./pages/organization/settings/SettingsPage";
import { OrgSettingsTabs } from "./pages/organization/settings/OrgSettingsTabs";
import { ORG_SETTINGS_TABS } from "./app/extensions/portal";
import { PreferencesPage } from "./pages/workspace/preferences/PreferencesPage";
import { SignupPage } from "./pages/auth/SignupPage";
import { TeamsPage } from "./pages/organization/teams/TeamsPage";
import { OAuthBusinessEmailPage } from "./pages/auth/OAuthBusinessEmailPage";
import { DeploymentPage } from "./pages/organization/org/DeploymentPage";
import { SsoSettingsPage } from "./pages/organization/org/SsoSettingsPage";
import { AuditLogPage } from "./pages/organization/org/AuditLogPage";
import { AdminOrgsPage } from "./pages/platform/admin/orgs/AdminOrgsPage";
import { AdminOpsPage } from "./pages/platform/admin/ops/AdminOpsPage";
import { AdminOrgRoute } from "./pages/platform/admin/orgs/AdminOrgDetailPage";
import { AdminUsersPage } from "./pages/platform/admin/users/AdminUsersPage";
import { AdminUserRoute } from "./pages/platform/admin/users/AdminUserDetailPage";
import { AdminBansPage } from "./pages/platform/admin/bans/AdminBansPage";
import { AdminAuditLogsPage } from "./pages/platform/admin/audit/AdminAuditLogsPage";

/** Files is code-split: the browser carries a virtualizer, an upload tray and a file-icon set no
 *  other page needs, so the shell only reaches the bundle when someone opens it. */
const FilesPage = lazy(() =>
  import("./pages/workspace/files/FilesPage").then((m) => ({ default: m.FilesScreen })),
);

/** Suspend on the Files chunk without flashing a spinner into the shell — the page's own
 *  skeletons take over the moment it mounts. */
function LazyFiles({ trash = false }: { trash?: boolean }) {
  return (
    <Suspense fallback={null}>
      <FilesPage trash={trash} />
    </Suspense>
  );
}

/** The admin console's landing page: an extension's, else the organizations register. */
function adminHome() {
  return ADMIN_HOME.items()[0]?.element ?? <Navigate to="/admin/orgs" replace />;
}

/** The routes an installed extension mounts at `mount`. */
function extensionRoutes(mount: RouteMount) {
  return PORTAL_ROUTES.items()
    .filter((route) => route.mount === mount)
    .map((route) => <Route key={route.key} path={route.path} element={route.element} />);
}

/** The app shell and routes, without a router. Exported so the design preview can mount
 *  it under a memory router; standalone, `App` wraps it in a BrowserRouter.
 *
 *  The product routes sit behind <RequireAuth>: signed out, they redirect to /login;
 *  signed in, they render under <AppLayout>. The auth pages stay outside the guard so
 *  a signed-out visitor can reach them. */
export function AppContent() {
  const settingsTabs = ORG_SETTINGS_TABS.items();
  return (
    <QueryClientProvider client={queryClient}>
      <UnauthorizedBridge />
      {/* Every tab of this browser follows the session into the org it is in. */}
      <SessionBridge />
      {/* The server event stream → cache invalidation, for as long as someone is signed in. */}
      <RealtimeBridge />
      <RealAuthActionsProvider>
        {/* The one writer of document.title — wraps the routes so a page can name
            what it is about (see app/documentTitle). */}
        <DocumentTitle>
        <Routes>
          <Route element={<RequireAuth />}>
            {/* Gate pages (no AppLayout): a name-less account finishes its profile here
                right after signup; an account past the email-verification grace window
                is held here until it confirms. Both sit inside the auth guard. */}
            <Route path="/complete-profile" element={<CompleteProfilePage />} />
            <Route path="/verify-email-required" element={<EmailVerificationGatePage />} />
            {/* A person in several orgs picks which one this browser enters. */}
            <Route path="/choose-org" element={<ChooseOrgPage />} />
            {/* An org's single sign-on, met by an email that already has an account, lands
                here once that account has signed in. Outside the deep-link gate: its `?org=`
                names the org being linked, not an org whose data the page shows. */}
            <Route path="/link-sso" element={<LinkSsoPage />} />
            <Route element={<OrgDeepLinkGate />}>
              <Route element={<AppLayout />}>
                <Route
                  path="/"
                  element={
                    <LiveDashboardProvider>
                      <DashboardPage />
                    </LiveDashboardProvider>
                  }
                />
                {/* The same chat the extension renders, over the cloud source rather than the
                    local daemon. /chat resolves to the chat this account last read; /chat/new is
                    the empty composer. All three render the same page so the hop from the
                    composer onto the chat it just created keeps the surface mounted; the id
                    route is guarded so an address that cannot name a chat never becomes a read. */}
                {/* One runtime for every chat surface, installed on the shared route so a cold
                    load of a stacked page (a plan, a compaction summary) has it too. */}
                <Route element={<ChatRuntimeLayout />}>
                  <Route path="/chat" element={<ChatPage />} />
                  <Route path="/chat/new" element={<ChatPage />} />
                  <Route path="/chat/:chatId" element={<ChatRoute />} />
                  {/* A workspace: its chats, files and people, in the same page as
                      its chats so the rail stays put between the two. */}
                  <Route path="/workspaces" element={<Navigate to="/chat" replace />} />
                  <Route path="/workspaces/:workspaceId" element={<WorkspaceRoute />} />
                  {/* The chat's stacked pages. The extension mounts them as editor tabs; the
                      portal has no tabs, so they live under their chat. One read of the chat
                      row guards them, so a stacked page over a missing chat says the chat is
                      gone rather than showing its own empty state. */}
                  <Route element={<ChatStackedRoute />}>
                    <Route path="/chat/:chatId/results" element={<BlobsSurface />} />
                    <Route path="/chat/:chatId/result/:handle" element={<BlobSurface />} />
                    <Route path="/chat/:chatId/plan/:partId" element={<PlanSurface />} />
                    <Route path="/chat/:chatId/compaction/:partId" element={<CompactionSurface />} />
                  </Route>
                </Route>
                {/* The org drive. /files carries no node and means "open my files": the shell
                    redirects to the caller's home once the root listing resolves it. Trash is a
                    static sibling, so it is declared before the node route. */}
                <Route path="/files" element={<LazyFiles />} />
                <Route path="/files/trash" element={<LazyFiles trash />} />
                {/* A malformed node id is rejected here, so it costs no request. */}
                <Route
                  path="/files/:nodeId"
                  element={
                    <FilesRoute>
                      <LazyFiles />
                    </FilesRoute>
                  }
                />
                {/* The Files tree lists saved queries and reports, so /objects lands there. */}
                <Route path="/objects" element={<Navigate to="/files" replace />} />
                {/* A saved query or a promoted result: one address, and the
                    object's own type picks the page (ObjectRoute). This is the
                    address a Files node's `object.web_url` carries. */}
                <Route path="/objects/:objectId" element={<ObjectRoute />} />
                {/* A chat template has its own address because its Files node carries
                    `/templates/<id>`, and that is the link people copy. */}
                <Route path="/templates/:templateId" element={<ChatTemplatePage />} />
                {/* One optional-param route so /teams ↔ /teams/:teamId share a mounted
                    page (selection changes don't remount the register). */}
                <Route path="/teams/:teamId?" element={<TeamsPage />} />
                <Route path="/settings/profile" element={<ProfileSettingsPage />} />
                {/* Personal preferences live outside the org's settings. /settings/chat still
                    resolves for bookmarks and the editor's "open your preferences" link. */}
                <Route path="/preferences" element={<PreferencesPage />} />
                <Route path="/settings/chat" element={<LegacyRedirect to="/preferences" />} />
                {/* Organization settings is one tabbed area, with General at the bare path so a
                    `#section` link lands on the page that owns the section. The org-admin guard
                    wraps the admin tabs inside the one layout rather than around a second copy. */}
                <Route path="/settings/organization" element={<OrgSettingsTabs />}>
                  <Route index element={<OrgSettingsPage />} />
                  {settingsTabs
                    .filter((tab) => !tab.orgAdmin)
                    .map((tab) => (
                      <Route key={tab.key} path={tab.path} element={tab.element} />
                    ))}
                  <Route element={<RequireOrgAdmin />}>
                    {settingsTabs
                      .filter((tab) => tab.orgAdmin)
                      .map((tab) => (
                        <Route key={tab.key} path={tab.path} element={tab.element} />
                      ))}
                    <Route path="sso" element={<SsoSettingsPage />} />
                    <Route path="audit" element={<AuditLogPage />} />
                  </Route>
                </Route>
                {/* The org's machines sit beside the settings tabs rather than in them: a team
                    admin manages the machines their teams own without the org settings a tab
                    strip would offer them. The list is cut server-side to what the reader may see. */}
                <Route path="/settings/organization/machines" element={<MachinesPage />} />
                <Route path="/settings/organization/machines/:machineId" element={<MachineDetailPage />} />
                {/* The in-shell verification page (resend + cooldown + re-check),
                    linked from the grace-window banner and opened by the CLI /
                    editor "verify your email" actions. Blocked-past-grace users
                    are bounced to /verify-email-required by RequireAuth. */}
                <Route path="/verify-email" element={<EmailVerificationPage />} />
                {extensionRoutes("member")}
                <Route path="*" element={<NotFoundPage />} />
              </Route>
              <Route element={<RequireOrgAdmin />}>
                <Route element={<AppLayout />}>
                  {/* Self-hosted-only: on the hosted app this bounces to the dashboard
                      (the nav already hides it and the backend 404s its endpoints). */}
                  <Route element={<RequireSelfHosted />}>
                    <Route path="/org/deployment" element={<DeploymentPage />} />
                  </Route>
                  {extensionRoutes("orgAdmin")}
                </Route>
              </Route>
              <Route element={<RequirePlatformStaff />}>
                <Route element={<AppLayout />}>
                  <Route path="/admin" element={adminHome()} />
                  <Route path="/admin/ops" element={<AdminOpsPage />} />
                  <Route path="/admin/orgs" element={<AdminOrgsPage />} />
                  <Route path="/admin/orgs/:orgId" element={<AdminOrgRoute />} />
                  <Route path="/admin/users" element={<AdminUsersPage />} />
                  <Route path="/admin/users/:userId" element={<AdminUserRoute />} />
                  {extensionRoutes("platformStaff")}
                  <Route element={<RequirePlatformAdmin />}>
                    {/* Banning an account or a domain locks someone out of the whole
                        product, so it sits with the audit log behind the higher grade. */}
                    <Route path="/admin/bans" element={<AdminBansPage />} />
                    <Route path="/admin/audit" element={<AdminAuditLogsPage />} />
                    {extensionRoutes("platformAdmin")}
                  </Route>
                </Route>
              </Route>
            </Route>
          </Route>
          {/* Older paths that emails (/dashboard/invites), Stripe returns and bookmarks still
              carry. Each redirect keeps the query and hash. */}
          <Route path="/dashboard" element={<LegacyRedirect to="/" />} />
          <Route path="/dashboard/teams" element={<LegacyRedirect to="/teams" />} />
          <Route
            path="/dashboard/teams/:teamId"
            element={<LegacyParamRedirect param="teamId" to={(teamId) => `/teams/${teamId}`} />}
          />
          <Route path="/dashboard/invites" element={<LegacyRedirect to="/teams?tab=invites" />} />
          <Route path="/dashboard/profile" element={<LegacyRedirect to="/settings/profile" />} />
          <Route
            path="/dashboard/org-settings"
            element={<LegacyRedirect to="/settings/organization" />}
          />
          {/* SSO and the audit log live in the org-settings tabs. */}
          <Route path="/org/sso" element={<LegacyRedirect to="/settings/organization/sso" />} />
          <Route path="/org/audit" element={<LegacyRedirect to="/settings/organization/audit" />} />
          <Route
            path="/dashboard/org/sso"
            element={<LegacyRedirect to="/settings/organization/sso" />}
          />
          <Route
            path="/dashboard/org/audit"
            element={<LegacyRedirect to="/settings/organization/audit" />}
          />
          <Route path="/dashboard/org/members" element={<LegacyRedirect to="/teams" />} />
          {extensionRoutes("public")}
          <Route path="/oauth/business-email" element={<OAuthBusinessEmailPage />} />
          <Route path="/login" element={<LoginPage />} />
          {/* A sign-in that found no org to enter. Public: the person holds a sign-in but no
              session in any org, so no guard could admit them. */}
          <Route path="/no-organization" element={<NoOrganizationPage />} />
          <Route path="/signup" element={<SignupPage />} />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/reset-password/:token" element={<ResetPasswordPage />} />
          {/* Public landing surfaces for the CLI / extension device grant and the
              verification-email link. /device self-redirects through /login when signed
              out (carrying ?user_code so the pre-filled approval survives). */}
          <Route path="/device" element={<DeviceApprovalPage />} />
          <Route path="/verify-email/:token" element={<VerifyEmailPage />} />
        </Routes>
        </DocumentTitle>
      </RealAuthActionsProvider>
    </QueryClientProvider>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <AppContent />
    </BrowserRouter>
  );
}
