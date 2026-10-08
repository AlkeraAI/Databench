"""The daemon binds a re-run's parameters with the SHARED compiler — the one
the objects routes validate with — so a value can never be rendered into the
statement on the daemon's side either."""

from __future__ import annotations

from alkera_cli.cloud import query_params
from alkera_core.schemas.objects import query_params as shared


def test_the_cloud_package_binds_through_the_shared_compiler() -> None:
    assert query_params.compile_query is shared.compile_query
    assert query_params.QueryCompileError is shared.QueryCompileError
    assert query_params.BoundQuery is shared.BoundQuery
    assert query_params.placeholders_of is shared.placeholders_of


def test_nothing_in_the_cloud_package_renders_a_literal() -> None:
    """The old renderer is gone: the daemon has no function that turns a value
    into SQL text."""
    assert not hasattr(query_params, "render_literal")
    assert not hasattr(query_params, "bind_query")
    bound = query_params.compile_query(
        "select * from orders where customer = {customer}", {"customer": "' OR 1=1 --"}, "tinybird"
    )
    assert bound.sql == "select * from orders where customer = {{String(customer)}}"
    assert bound.params == {"customer": "' OR 1=1 --"}
