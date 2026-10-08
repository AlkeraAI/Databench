import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import { Tabs } from "@alkera/ui";

import { useIdentityDashboard } from "../../../api/dashboard";
import { OrgPage } from "./chrome";
import { HealthPanel } from "./deployment/HealthPanel";
import { ModelProvidersPanel } from "./deployment/ModelProvidersPanel";

/**
 * The self-hosted operator's Deployment surface. Tab 1 (Health) checks the whole
 * install is green; Tab 2 (Model providers) is the BYOK key management, shown only
 * when the deployment is entitled to BYOK. Both sit on the shared OrgPage chrome.
 */
export function DeploymentPage() {
  const entitled = useIdentityDashboard().data?.entitled_features ?? [];
  const byok = entitled.includes("byok");
  const [params, setParams] = useSearchParams();

  const items = useMemo(
    () => [
      { key: "health", label: "Health" },
      ...(byok ? [{ key: "providers", label: "Model providers" }] : []),
    ],
    [byok],
  );

  const requested = params.get("tab") ?? "health";
  const tab = items.some((i) => i.key === requested) ? requested : "health";

  return (
    <OrgPage subtitle="Check your self-hosted install is healthy and manage provider keys.">
      {(notify) => (
        <>
          <Tabs
            items={items}
            value={tab}
            onChange={(key) => setParams({ tab: key }, { replace: true })}
            label="Deployment sections"
          />
          {tab === "providers" && byok ? <ModelProvidersPanel notify={notify} /> : <HealthPanel />}
        </>
      )}
    </OrgPage>
  );
}
