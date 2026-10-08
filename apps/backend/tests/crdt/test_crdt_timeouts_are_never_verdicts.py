"""A timeout or a cancellation is never a verdict on what a client sent.

Only the validator's own refusal, or a worker dying on the same bytes twice,
may refuse or poison an update. A timeout says the host was slow (a loaded
machine, a cold worker), and a cancellation says the caller left: both leave
the edit the sender's to send again. A first SQL cell on a cold worker once
timed out three times, was poisoned, closed the socket, and the tab dropped
the cell with its text.

The gate holds the rule two ways:

* by construction: every sandbox request in the backend goes through one
  seam (``CrdtDocs.sandbox_request``), which answers a timeout ``crdt_busy``
  for every operation the lane sends, and no handler of a timeout or a
  cancellation anywhere in the lane refuses or poisons;
* by behaviour: through that seam, a timeout on every operation is busy with
  a wait, and never touches the poison list.

The store-level and socket-level proofs (busy every time, the same bytes
landing once the sandbox keeps up, a cancelled write landing when resent, a
timeout never a strike) live in ``test_crdt_docs.py`` and
``test_crdt_socket_crashes.py``.
"""

from __future__ import annotations

import ast
import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest
from backend.services.crdt.docs import CrdtDocs, CrdtError
from backend.services.crdt.registry import DocRef
from backend.services.crdt.sandbox.pool import SandboxCrashedError, SandboxTimeoutError

BACKEND = Path(__file__).resolve().parents[2] / "backend"
LANE = BACKEND / "services" / "crdt"

#: Every operation the backend asks a sandbox worker for.
OPS = (
    "seed",
    "load",
    "catchup",
    "validate",
    "advance",
    "export",
    "content",
    "latest",
    "merge",
    "ephemeral",
    "salvage",
    "ops",
    "graph",
    "normalize",
)


def _sources(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _function_of(tree: ast.AST, node: ast.AST) -> str:
    """The name of the innermost function holding ``node``."""
    found = ""
    for candidate in ast.walk(tree):
        if not isinstance(candidate, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if any(child is node for child in ast.walk(candidate)):
            found = candidate.name
    return found


def test_every_sandbox_request_goes_through_the_one_seam() -> None:
    """The pool is asked for work in one place, so one place decides what a
    timeout means. A second path to the pool would be a second answer."""
    callers = []
    for path in _sources(BACKEND):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            target = node.func.value
            names_pool = isinstance(target, ast.Attribute) and target.attr == "pool"
            if node.func.attr == "request" and names_pool:
                callers.append((path.relative_to(BACKEND).as_posix(), _function_of(tree, node)))
    assert callers == [("services/crdt/docs.py", "sandbox_request")]


def _handled(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"BaseException"}
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {ast.unparse(kind).rsplit(".", 1)[-1] for kind in kinds}


def _verdicts_in(handler: ast.ExceptHandler) -> list[str]:
    """What in ``handler``'s body refuses or poisons: a ``crdt_rejected``
    code, a write to the poison list, a strike against the sender."""
    found = []
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Constant) and node.value == "crdt_rejected":
            found.append("crdt_rejected")
        if isinstance(node, ast.Attribute) and node.attr in ("_poison", "_count_crash"):
            found.append(node.attr)
    return found


def test_no_handler_of_a_timeout_or_a_cancellation_refuses_or_poisons() -> None:
    timeouts = {"SandboxTimeoutError", "TimeoutError", "CancelledError", "BaseException"}
    offenders = []
    for path in _sources(LANE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _handled(node) & timeouts:
                verdicts = _verdicts_in(node)
                if verdicts:
                    offenders.append((path.relative_to(LANE).as_posix(), node.lineno, verdicts))
    assert offenders == []


def test_a_timeout_is_no_kind_of_crash() -> None:
    """Crash handlers retry once and then poison the bytes; a timeout must
    never be caught by one."""
    assert not issubclass(SandboxTimeoutError, SandboxCrashedError)


class _TimingOut:
    """A pool whose every request outlives its budget."""

    def __init__(self) -> None:
        self.sent = 0

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        self.sent += 1
        raise SandboxTimeoutError("the sandbox worker timed out")


@pytest.mark.parametrize("op", OPS)
async def test_a_timeout_on_any_operation_is_busy_with_a_wait_never_a_refusal(op: str) -> None:
    pool = _TimingOut()
    now = [1000.0]
    docs = CrdtDocs(pool=pool, clock=lambda: now[0])  # type: ignore[arg-type]
    ref = DocRef(org_id=uuid.uuid4(), doc_type="notebook", doc_id="node-1")
    update = b"\x00 an update"
    digest = hashlib.sha256(update).digest()
    try:
        for _ in range(3):
            with pytest.raises(CrdtError) as busy:
                await docs.sandbox_request(ref, {"op": op}, [update], budget=0.1, slow_key=digest)
            assert busy.value.code == "crdt_busy"
            assert (busy.value.retry_after_ms or 0) > 0
            now[0] += (busy.value.retry_after_ms or 0) / 1000
        assert pool.sent == 3
        assert not docs._poisoned(digest)
    finally:
        await docs.aclose()
