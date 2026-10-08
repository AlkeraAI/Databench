"""Who may bind a session to its orgs, and who may step out of that binding.

Row-level security on the content tables holds a request to its orgs only
because its session is bound (``alkera_core.db.tenant_session.bind_tenant``)
at the one place every credential passes through, and because nothing steps
out of the binding except through the two audited seams in
``alkera_core.db.cross_tenant``. Both are facts about the source tree, so they
are read from it: a new ``bind_tenant`` call site, or a new cross-tenant
window, fails here until it is named below with its reason.

Pure AST reads over the backend, the worker, the gateway and ``alkera_core``.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests._tenancy_architecture_scan import REPO, SCOPES

#: The only modules that may call ``bind_tenant``: the principal dependency
#: path, where every credential shape resolves to its acting context.
BIND_CALLERS = frozenset({"backend.auth.dependencies"})

#: Every cross-tenant window in the codebase: (module, seam, reason). The
#: reason is the literal ``reason=`` the call passes, which is also what its
#: ``db.cross_tenant_read`` / ``db.cross_tenant_write`` log line names.
CROSS_TENANT_CALLERS: frozenset[tuple[str, str, str]] = frozenset(
    {
        # The Files janitor, collector and recovery sweep enumerate every org.
        ("worker.tasks.files", "cross_tenant_read", "files.janitor.orgs_with_drives"),
        ("worker.tasks.files", "cross_tenant_read", "files.gc.known_domains"),
        ("worker.tasks.files", "cross_tenant_read", "files.gc.adopt"),
        ("worker.tasks.files", "cross_tenant_read", "files.recover_queued.orgs"),
        # Platform staff act across every org; the admin router holds each
        # request in one window (which staff may do what, the policies decide).
        ("backend.api.admin", "cross_tenant_write", "admin.platform_surface"),
        # A person joining an org from a session in another: the seat the join
        # writes is in the org being joined, which the session is not bound to.
        ("backend.api.routes.identity.org_sessions", "cross_tenant_write", "membership.join"),
        ("backend.services.identity.sso_link", "cross_tenant_write", "sso_link.join"),
        # The orgs a person may switch into, with their role in each: their own
        # root-team admin rows in the orgs the session is not bound to.
        (
            "backend.services.identity.org_choice",
            "cross_tenant_write",
            "org_choice.own_admin_roles",
        ),
        (
            "backend.services.org.invitations",
            "cross_tenant_write",
            "invitation.accept_into_another_org",
        ),
        # Founding an org from a session in another: the new org's rows belong
        # to an org that did not exist when the request bound.
        ("backend.services.org.own_orgs", "cross_tenant_write", "org.found_for_identity"),
        # A by-id load that the request's orgs cannot see is read once more by
        # id so the engine refuses the cross-org probe and leaves it on record.
        ("backend.services.sharing.access", "cross_tenant_write", "objects.load_by_id.probe"),
        (
            "backend.services.workspaces.workspace_service",
            "cross_tenant_write",
            "workspaces.load_by_id.probe",
        ),
        (
            "backend.services.workspaces.workspace_service",
            "cross_tenant_write",
            "workspaces.load_ended_by_id.probe",
        ),
        # A box on its machine credential names a drive; which org it is in is
        # read across orgs, and the credential's serves() check decides next.
        ("backend.api.deps.files_context", "cross_tenant_write", "files.machine.drive_named"),
        ("backend.services.files.context", "cross_tenant_write", "files.machine.drive_lookup"),
        # A box on its machine credential names a node (a live document); the
        # drive it is on is read across orgs, then admitted as a named drive is.
        ("backend.services.files.context", "cross_tenant_write", "files.machine.node_lookup"),
        # A machine's chats span every org it serves: restating them as the box
        # sleeps, wakes or leaves, counting its load, ending what it held, and
        # moving stranded chats onto a box that came up.
        ("alkera_core.compute.handoff", "cross_tenant_write", "compute.machine_chats.restate"),
        ("alkera_core.compute.handoff", "cross_tenant_write", "compute.machine_chats.count"),
        ("alkera_core.objects.chat_end", "cross_tenant_write", "compute.machine_chats.live"),
        ("alkera_core.objects.chat_spares", "cross_tenant_write", "compute.machine_chats.spares"),
        (
            "backend.services.compute.machines",
            "cross_tenant_write",
            "compute.machine_chats.bound_counts",
        ),
        ("backend.services.compute.machines", "cross_tenant_write", "compute.machine_chats.load"),
        (
            "backend.services.compute.placement",
            "cross_tenant_write",
            "compute.placement.rescue_stranded",
        ),
        # Org machines are platform-driven: the reconcile converges every org's
        # machines to what each org asked for. The meter that bills them is the
        # product's, and its windows are listed with the product.
        (
            "alkera_core.compute.org_reconcile",
            "cross_tenant_read",
            "compute.org_machines.reconcile.list",
        ),
        (
            "alkera_core.compute.org_reconcile",
            "cross_tenant_read",
            "compute.org_machines.reconcile.idle",
        ),
        (
            "alkera_core.compute.org_reconcile",
            "cross_tenant_write",
            "compute.org_machines.reconcile",
        ),
        # A start asked with no row held records what the provider answered
        # in a transaction of its own, across orgs like the reconcile.
        (
            "alkera_core.compute.org_reconcile",
            "cross_tenant_write",
            "compute.org_machines.reconcile.start",
        ),
        # The notebook run sweep ends every org's runs no engine will end.
        ("worker.tasks.notebooks", "cross_tenant_write", "notebooks.sweep_runs"),
        (
            "alkera_core.compute.org_reconcile",
            "cross_tenant_write",
            "compute.org_machines.reconcile.announce",
        ),
        (
            "alkera_core.compute.org_reconcile",
            "cross_tenant_write",
            "compute.org_machines.reconcile.idle_stop",
        ),
        # The recovery sweep finds every org's quiet workspace moves.
        (
            "alkera_core.compute.workspace_move",
            "cross_tenant_read",
            "workspace.machine_move.recover",
        ),
        # The overdue sweep ends every org's move that outsat its bound.
        (
            "alkera_core.compute.workspace_move",
            "cross_tenant_read",
            "workspace.machine_move.overdue",
        ),
        # Platform staff give an org a machine: its rows belong to that org.
        ("backend.services.compute.offerings", "cross_tenant_write", "admin.machine_grant"),
        # A person's own account spans every org they belong to: the deletion
        # plan, the wind-down of their credentials and chats, and the erasure
        # each read or write that person's rows in all of them, and the plan
        # names the orgs and people it lists.
        ("alkera_core.account.plan", "cross_tenant_write", "account.plan"),
        ("alkera_core.account.erasure", "cross_tenant_write", "account.wind_down"),
        ("alkera_core.account.erasure", "cross_tenant_write", "account.erase"),
        (
            "backend.services.identity.account_lifecycle",
            "cross_tenant_write",
            "account.plan_names",
        ),
    }
)

#: The product's windows (the compute meter's legs), kept with the product.
#: Absent from the open tree, where the scan finds none of them.
PRODUCT_WINDOWS = Path(__file__).parent / "fixtures" / "tenant_windows_product.json"


def _expected_windows() -> frozenset[tuple[str, str, str]]:
    """The open windows, and the product's when the product is present."""
    if not PRODUCT_WINDOWS.exists():
        return CROSS_TENANT_CALLERS
    listed = json.loads(PRODUCT_WINDOWS.read_text(encoding="utf-8"))["windows"]
    return CROSS_TENANT_CALLERS | {(w["module"], w["seam"], w["reason"]) for w in listed}


_SEAMS = frozenset({"cross_tenant_read", "cross_tenant_write"})


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for directory, package in SCOPES:
        root = REPO / directory
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.relative_to(root).parts:
                continue
            relative = path.relative_to(root).with_suffix("")
            parts = [package, *relative.parts]
            if parts[-1] == "__init__":
                parts.pop()
            yield ".".join(parts), ast.parse(Path(path).read_text(), filename=str(path))


def _called(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _reason(node: ast.Call) -> str:
    for keyword in node.keywords:
        if keyword.arg == "reason":
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value
            return "<not a literal>"
    return "<missing>"


def _calls(name: str) -> Iterator[tuple[str, ast.Call]]:
    for module, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called(node) == name:
                yield module, node


def test_only_the_principal_path_binds_a_session() -> None:
    callers = {module for module, _ in _calls("bind_tenant")} - {"alkera_core.db.tenant_session"}
    assert callers == BIND_CALLERS


def test_every_cross_tenant_window_is_named_with_its_reason() -> None:
    found = {
        (module, seam, _reason(node))
        for seam in _SEAMS
        for module, node in _calls(seam)
        if module != "alkera_core.db.cross_tenant"
    }
    assert found == _expected_windows()


#: The modules allowed to spell a step out to the login. ``tenant_session``
#: names the login (``LOGIN_ROLE``) and answers it as the outer role of a
#: session nobody bound or one inside a cross-tenant window; the cross-tenant
#: seam is the one place that steps a bound session out to it. Code that leaves
#: a narrower role (the Files role) goes through ``tenant_session.stepped_out``,
#: which picks the outer role, so it never names the login itself.
LOGIN_STEP_OUT_SPELLERS = frozenset(
    {
        "alkera_core.db.cross_tenant",
        "alkera_core.db.tenant_session",
    }
)

#: The constant ``tenant_session`` names the login by.
_LOGIN_NAME = "LOGIN_ROLE"
#: The calls that set the role to the value they are handed.
_ROLE_SETTERS = frozenset({"swap_role", "restore_role"})


def _spells_a_login_step_out(literal: str) -> bool:
    folded = " ".join(literal.upper().split())
    return "ROLE NONE" in folded or "RESET ROLE" in folded or "'ROLE', 'NONE'" in folded


def _docstrings(tree: ast.Module) -> set[int]:
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _names_the_login(node: ast.AST) -> bool:
    """A reference to ``LOGIN_ROLE``, or a role setter handed ``"none"``."""
    if isinstance(node, ast.Name) and node.id == _LOGIN_NAME:
        return True
    if isinstance(node, ast.Attribute) and node.attr == _LOGIN_NAME:
        return True
    if isinstance(node, ast.Call) and _called(node) in _ROLE_SETTERS:
        return any(
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and arg.value.strip().lower() == "none"
            for arg in node.args[1:]
        )
    return False


def _steps_out_to_the_login(tree: ast.Module) -> bool:
    prose = _docstrings(tree)
    return any(
        _names_the_login(node)
        or (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in prose
            and _spells_a_login_step_out(node.value)
        )
        for node in ast.walk(tree)
    )


def _login_step_out_spellers() -> set[str]:
    return {module for module, tree in _modules() if _steps_out_to_the_login(tree)}


def test_nothing_else_steps_out_to_the_login() -> None:
    assert _login_step_out_spellers() == LOGIN_STEP_OUT_SPELLERS


def test_the_scan_sees_the_calls_it_gates() -> None:
    """A scan that read nothing would pass both gates; it must find the one
    binding it knows is there."""
    assert any(module == "backend.auth.dependencies" for module, _ in _calls("bind_tenant"))


@pytest.mark.parametrize(
    ("source", "spells"),
    [
        pytest.param('await swap_role(s, "none")', True, id="a-setter-handed-none"),
        pytest.param("await restore_role(s, LOGIN_ROLE)", True, id="a-setter-handed-the-login"),
        pytest.param("role = tenant_session.LOGIN_ROLE", True, id="the-login-by-attribute"),
        pytest.param('await s.execute(text("SET LOCAL ROLE NONE"))', True, id="raw-sql"),
        pytest.param("async with stepped_out(s): pass", False, id="the-shared-step-out"),
        pytest.param('await swap_role(s, "alkera_files_app")', False, id="a-narrower-role"),
    ],
)
def test_the_step_out_scan_knows_the_login_when_it_sees_it(source: str, spells: bool) -> None:
    tree = ast.parse(f"async def f():\n    {source}\n")
    assert _steps_out_to_the_login(tree) is spells
