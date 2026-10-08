"""The compute plane's process-independent core.

Everything here is importable by the backend AND the worker (which ships
without the backend package): the provider interface and its RunPod
implementation, the per-minute meter, the catalog refresh, the SSH keypair
helpers and the reachability rules a workspace machine is judged by. The
HTTP layer, admission (grants) and placement live in
``backend.services.compute``.
"""

from __future__ import annotations
