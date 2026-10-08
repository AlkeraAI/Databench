"""The shared engine's pool geometry is a declared capacity, not an inherited default.

One uvicorn process serves every tenant on one event loop, ``get_db`` holds a
connection for the WHOLE request (so any external call inside a handler holds one
too), and ``/health/ready`` probes through the SAME pool — so the pool's size and
its wait are what decide whether a busy process degrades or gets deregistered by
the load balancer. SQLAlchemy's defaults (5 + 10 overflow, a 30 s wait) are a
library choice, never a decision about this app, and nothing else in the suite
would notice if they silently became the configuration again.
"""

from __future__ import annotations

from alkera_core.db import session as db_session


def test_the_pool_is_explicitly_sized_not_inherited() -> None:
    """The engine is built with the module's declared geometry. Assert against the
    LIVE pool, not the constants — a change that edits the constants but forgets to
    pass them through would still leave the defaults in force."""
    pool = db_session.engine.pool
    assert pool.size() == db_session.POOL_SIZE
    assert pool._max_overflow == db_session.MAX_OVERFLOW
    assert pool._timeout == db_session.POOL_TIMEOUT_SECONDS


def test_the_declared_geometry_beats_sqlalchemys_defaults() -> None:
    """The point of declaring it: more room than the 15-connection / 30-second
    default, and a wait short enough that a saturated pool sheds instead of
    piling up half-minute-long requests that each hold a server slot.

    Asserted against the settings DEFAULTS, not the live constants — the
    geometry is env-tunable now (a service may legitimately run a small pool,
    e.g. the gateway's 12+6), so the ambient environment must not flip this
    test; what it pins is the shipped default."""
    from alkera_core.config import Settings

    fields = Settings.model_fields
    assert fields["database_pool_size"].default + fields["database_pool_max_overflow"].default > 15
    assert db_session.POOL_TIMEOUT_SECONDS < 30.0
