"""Account lifecycle: account erasure.

``plan`` decides what deleting an account does, ``dispositions`` names what
erasure does to every column that holds a person, ``erasure`` runs it,
``requests`` creates and cancels requests, ``lifecycle`` is the background work
the worker drives, and ``contributors`` is how another domain takes part.
Shared by the backend and the worker, which depends on ``alkera-core`` only.
"""
