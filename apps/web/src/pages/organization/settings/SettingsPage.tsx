import { SettingsDoc } from "./fields";
import { OrgBody } from "./OrgBody";
import { ProfileBody } from "./ProfileBody";

/**
 * Settings — the account ledger. Two pages reached from the shell account popover: Profile (the
 * person's own account) and Organization (org-admin controls). Each is the shared SettingsDoc shell
 * around one body, mirroring the real apps/web ProfilePage / OrgSettingsPage. The reader's own
 * Preferences is a third page on the same shell, routed from pages/workspace/preferences.
 */
export function ProfileSettingsPage() {
  return (
    <SettingsDoc>
      {(notify) => <ProfileBody notify={notify} />}
    </SettingsDoc>
  );
}

export function OrgSettingsPage() {
  return (
    <SettingsDoc>
      {(notify) => <OrgBody notify={notify} />}
    </SettingsDoc>
  );
}
