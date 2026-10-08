"""Workflow definitions, one module per job family.

``worker.temporal.queues`` walks this package and registers every
``@workflow.defn`` class it finds under the queue the shared contract assigns
to its type name, so a new family is a new module here and nothing else.
Modules whose name starts with an underscore are shared helpers (the drain
loop) and are not walked.
"""
