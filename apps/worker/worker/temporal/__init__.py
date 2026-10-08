"""The worker's Temporal runtime: retry policies, the activity interceptor, the
queue registries and the process that serves them.

The shared contract (queues, workflow types, ids) and the client factory live
in ``alkera_core.temporal``; this package holds only what a *serving* process
needs, so the backend never imports it.
"""
