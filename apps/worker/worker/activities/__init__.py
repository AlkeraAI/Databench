"""Activity definitions, one module per job family.

Each module holds only ``@activity.defn`` wrappers around the async cores in
``worker.tasks``; ``worker.temporal.queues`` walks this package and registers
every activity under the queue its type name maps to, so a new family is a new
module here and nothing else.
"""
