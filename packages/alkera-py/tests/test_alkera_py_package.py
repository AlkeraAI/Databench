"""The ``alkera`` distribution installs the ``alkera.test`` data-test API from
its own package directory, not from the CLI's tree it used to live in."""

from __future__ import annotations

from pathlib import Path

import alkera
import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_alkera_resolves_from_the_alkera_py_distribution() -> None:
    assert Path(alkera.__file__).resolve().parent == PACKAGE_ROOT / "alkera"


def test_registered_test_round_trips_through_the_public_api() -> None:
    alkera.reset_registered()
    try:

        @alkera.test(refs=["duckdb://shop/main.orders#amount"], severity="warning")
        def amounts_positive(conn: alkera.Connection) -> None:
            assert conn.sql("select 1").is_empty()

        (registered,) = alkera.registered_tests()
        assert registered.fn is amounts_positive
        assert registered.refs == ("duckdb://shop/main.orders#amount",)
        assert registered.severity == "warning"
        assert registered.name == "amounts_positive"
        assert registered.source_file == __file__
    finally:
        alkera.reset_registered()
    assert alkera.registered_tests() == []


@pytest.mark.parametrize(
    "refs",
    [
        pytest.param("duckdb://shop/main.orders", id="bare-string"),
        pytest.param([], id="empty"),
        pytest.param([""], id="blank-ref"),
    ],
)
def test_bad_refs_are_refused(refs: object) -> None:
    with pytest.raises(ValueError, match="refs="):
        alkera.test(refs=refs)  # type: ignore[arg-type]


def test_rowset_reports_emptiness_and_samples() -> None:
    rows = alkera.RowSet([{"id": i} for i in range(7)])
    assert not rows.is_empty()
    assert len(rows) == 7
    assert rows.sample(2) == [{"id": 0}, {"id": 1}]
    assert alkera.RowSet([]).is_empty()
