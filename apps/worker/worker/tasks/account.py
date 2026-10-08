"""Account lifecycle: the async cores behind the sweep and the re-erasure.

The logic lives in ``alkera_core.account.lifecycle``, shared with nothing else
but written so a test can drive it with no worker; these cores only supply the
email sender that delivers the lifecycle's notices.
"""

from __future__ import annotations

from alkera_core.account import ledger, lifecycle
from alkera_core.email.account import send_account_notice


async def _lifecycle_sweep() -> dict[str, int]:
    """Due erasures. Returns what it did."""
    report = await lifecycle.sweep(notify=send_account_notice)
    return {f"deletions_{status}": n for status, n in report.deletions.items()}


async def _reerase() -> dict[str, int]:
    """Erase again every ledgered identity a restore brought back."""
    report = await ledger.reerase()
    return {
        "ledgered": report.ledgered,
        "resurrected": report.resurrected,
        "erased": len(report.erased),
        "failed": len(report.failed),
    }
