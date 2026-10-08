// The chat banner for a workspace pinned to an org machine: the machine's state
// in its own name, with the way on (start it again, move the
// workspace). Anything the pinned machine has nothing to say about falls back
// to the chat's own machine banner.

import { useState, type ReactElement, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import { useMachinePower, useOrgMachines, useWorkspaceMachine } from "../../../api/machines";
import { useIdentityDashboard } from "../../../api/dashboard";
import { machineBilling } from "../../../app/extensions/portal";
import { useNow } from "../../organization/machines/useNow";
import { ChangeMachineDialog } from "../workspaces/WorkspaceMachine";

import { PinnedMachineBannerView, pinnedMachineCopy } from "./MachineBanner";

export function PinnedMachineBanner({
  workspaceId,
  fallback,
}: {
  workspaceId: string | undefined;
  fallback: ReactNode;
}): ReactElement {
  const read = useWorkspaceMachine(workspaceId);
  const card = read.data?.card;
  const pinned = card?.kind === "org_machine" ? card.org_machine_id ?? null : null;
  // The machine's own read says whether this reader manages it.
  const machines = useOrgMachines(pinned !== null);
  const machine = machines.data?.find((m) => m.id === pinned);
  const isOrgAdmin = useIdentityDashboard().data?.is_org_admin === true;
  const power = useMachinePower();
  const navigate = useNavigate();
  const [moving, setMoving] = useState(false);
  const now = useNow(card?.state === "starting");

  const copy =
    card && read.data
      ? pinnedMachineCopy(card, {
          manager: machine?.can_manage === true,
          mayAddCredits: isOrgAdmin,
          canMove: read.data.can_move,
          now,
        })
      : null;
  if (!copy || !read.data || !workspaceId) return <>{fallback}</>;

  return (
    <>
      <PinnedMachineBannerView
        copy={copy}
        onAction={(action) => {
          const extended = machineBilling()?.pinnedActions[action];
          if (action === "change_machine") setMoving(true);
          else if (extended) extended.run(navigate);
          else if (machine) power.mutate({ machineId: machine.id, version: machine.version, action: "start" });
        }}
      />
      <ChangeMachineDialog open={moving} onClose={() => setMoving(false)} workspaceId={workspaceId} read={read.data} />
    </>
  );
}
