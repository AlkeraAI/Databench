"""The backend's authorization choke point.

A route resolves its facts, calls :func:`enforce`, and either continues (the
allow is on record in its own transaction) or is already answering a 403 / 404
(the deny is on record in a transaction of its own). A route that decides over
more than one resource calls :func:`decide_many` and filters. See
:mod:`.enforce` and :mod:`.batch`.
"""

from __future__ import annotations

from backend.authz.batch import (
    AttrsFor,
    BatchFacts,
    batch_facts_for,
    decide_many,
    register_batch_facts,
)
from backend.authz.enforce import (
    DecisionSink,
    OutboxDecisionSink,
    SettledRepeatSink,
    decide_on_record,
    default_sink,
    enforce,
    http_error_for,
    role_resolver,
)

__all__ = [
    "AttrsFor",
    "BatchFacts",
    "DecisionSink",
    "OutboxDecisionSink",
    "SettledRepeatSink",
    "batch_facts_for",
    "decide_many",
    "decide_on_record",
    "default_sink",
    "enforce",
    "http_error_for",
    "register_batch_facts",
    "role_resolver",
]
