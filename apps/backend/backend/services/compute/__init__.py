"""The compute plane's HTTP-side services: admission (grants), the session
allocation lifecycle, the org's workspace machine, chat placement, the machine
offerings and the org machines bought from them. The
worker-importable core — provider, meter, refresh, reachability — lives in
``alkera_core.compute``; this package sits on top of it."""

from __future__ import annotations

from backend.services.compute.box_logs import ingest as ingest_box_logs
from backend.services.compute.grants import (
    ComputeLimitReachedError,
    ComputeRefusedError,
    Grant,
    InsufficientComputeCreditError,
    NoComputeGrantError,
    admit,
    resolve_compute_grant,
)
from backend.services.compute.localdev_keepalive import start as start_local_box_keepalive
from backend.services.compute.localdev_keepalive import stop as stop_local_box_keepalive
from backend.services.compute.machine_lost import LostMachine, lost_machine, lost_machine_of_chat
from backend.services.compute.machines import machine_state_read
from backend.services.compute.offerings import (
    OfferingError,
    admin_offering,
    admin_offerings,
    assign_dedicated,
    box_revoked,
    can_offer,
    create_offering,
    grant_machine,
    granted_pool_machine,
    legacy_assignment_org,
    offering_read,
    unassign_dedicated,
    update_offering,
    visible_offerings,
)
from backend.services.compute.org_admission import admit_org_machine_start
from backend.services.compute.org_compute_settings import read_settings as read_org_compute_settings
from backend.services.compute.org_compute_settings import (
    update_settings as update_org_compute_settings,
)
from backend.services.compute.org_machine_access import MachineRow as OrgMachineRow
from backend.services.compute.org_machine_access import OrgMachineError
from backend.services.compute.org_machine_access import Viewer as OrgMachineViewer
from backend.services.compute.org_machine_access import allowed as org_machine_allowed
from backend.services.compute.org_machine_access import audiences_of as org_machine_audiences
from backend.services.compute.org_machine_access import load_row as load_org_machine
from backend.services.compute.org_machine_access import load_rows as load_org_machines
from backend.services.compute.org_machine_access import machine_attrs as org_machine_attrs
from backend.services.compute.org_machine_access import resource_for as org_machine_resource
from backend.services.compute.org_machine_access import viewer_for as org_machine_viewer
from backend.services.compute.org_machine_buying import buyer_offerings
from backend.services.compute.org_machine_buying import buying as org_machine_buying_read
from backend.services.compute.org_machine_buying import plan_purchase as plan_org_machine_purchase
from backend.services.compute.org_machine_buying import purchase as purchase_org_machine
from backend.services.compute.org_machine_buying import quote as quote_org_machine
from backend.services.compute.org_machine_reads import detail as org_machine_detail
from backend.services.compute.org_machine_reads import org_machine_card
from backend.services.compute.org_machine_reads import (
    platform_read_models as platform_org_machine_reads,
)
from backend.services.compute.org_machine_reads import read_models as org_machine_read_models
from backend.services.compute.org_machines import delete as delete_org_machine
from backend.services.compute.org_machines import grow_disk as grow_org_machine_disk
from backend.services.compute.org_machines import quote_disk_grow as quote_org_machine_disk_grow
from backend.services.compute.org_machines import replace as replace_org_machine
from backend.services.compute.org_machines import replace_audience as replace_org_machine_audience
from backend.services.compute.org_machines import start as start_org_machine
from backend.services.compute.org_machines import stop as stop_org_machine
from backend.services.compute.org_machines import update as update_org_machine
from backend.services.compute.placement import (
    UNSERVEABLE,
    CapabilityMissingError,
    LiveMachines,
    MachineBinding,
    bound_machine_state,
    chat_is_writable,
    chat_machine_status,
    live_machines,
    new_workspace_pin,
    org_pool_applies,
    place_new_chat,
    rebind_if_stranded,
    resolve_machine_for,
    team_ids_of,
    workspace_pin,
)
from backend.services.compute.provisioning import ProvisionError
from backend.services.compute.service import MachineTypeUnavailableError
from backend.services.compute.ssh_machines import add as add_ssh_machine
from backend.services.compute.ssh_machines import add_attrs as ssh_machine_add_attrs
from backend.services.compute.ssh_machines import can_add as can_add_org_machine
from backend.services.compute.ssh_machines import default_transport as default_ssh_transport
from backend.services.compute.ssh_machines import test_connection as test_ssh_machine

__all__ = [
    "UNSERVEABLE",
    "CapabilityMissingError",
    "ComputeLimitReachedError",
    "ComputeRefusedError",
    "Grant",
    "InsufficientComputeCreditError",
    "LiveMachines",
    "LostMachine",
    "MachineBinding",
    "MachineTypeUnavailableError",
    "NoComputeGrantError",
    "OfferingError",
    "OrgMachineError",
    "OrgMachineRow",
    "OrgMachineViewer",
    "ProvisionError",
    "add_ssh_machine",
    "admin_offering",
    "admin_offerings",
    "admit",
    "admit_org_machine_start",
    "assign_dedicated",
    "bound_machine_state",
    "box_revoked",
    "buyer_offerings",
    "can_add_org_machine",
    "can_offer",
    "chat_is_writable",
    "chat_machine_status",
    "create_offering",
    "default_ssh_transport",
    "delete_org_machine",
    "grant_machine",
    "granted_pool_machine",
    "grow_org_machine_disk",
    "ingest_box_logs",
    "legacy_assignment_org",
    "live_machines",
    "load_org_machine",
    "load_org_machines",
    "lost_machine",
    "lost_machine_of_chat",
    "machine_state_read",
    "new_workspace_pin",
    "offering_read",
    "org_machine_allowed",
    "org_machine_attrs",
    "org_machine_audiences",
    "org_machine_buying_read",
    "org_machine_card",
    "org_machine_detail",
    "org_machine_read_models",
    "org_machine_resource",
    "org_machine_viewer",
    "org_pool_applies",
    "place_new_chat",
    "plan_org_machine_purchase",
    "platform_org_machine_reads",
    "purchase_org_machine",
    "quote_org_machine",
    "quote_org_machine_disk_grow",
    "read_org_compute_settings",
    "rebind_if_stranded",
    "replace_org_machine",
    "replace_org_machine_audience",
    "resolve_compute_grant",
    "resolve_machine_for",
    "ssh_machine_add_attrs",
    "start_local_box_keepalive",
    "start_org_machine",
    "stop_local_box_keepalive",
    "stop_org_machine",
    "team_ids_of",
    "test_ssh_machine",
    "unassign_dedicated",
    "update_offering",
    "update_org_compute_settings",
    "update_org_machine",
    "visible_offerings",
    "workspace_pin",
]
