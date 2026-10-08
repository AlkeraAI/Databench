"""The freezegun module ignore list is configured for EVERY suite, not just the CLI's.

``freeze_time`` sweeps every loaded module to patch its clock references; the
warehouse driver stacks and the Temporal SDK (a Rust core behind a large generated
tree) must be skipped or the sweep hard-crashes the Windows workers. The list used to
live in the CLI conftest, which the CI shard that runs THIS suite (backend / worker /
api-core) never loads — so the packages those suites import were unprotected. The
configuration now lives in the repo-root conftest; this test, which lives outside
the CLI tree, proves it reaches here.
"""

from __future__ import annotations

import freezegun.config
import pytest

# Every package the root conftest must shield from the freeze_time sweep.
_EXPECTED_IGNORED = (
    "clickhouse_connect",
    "duckdb",
    "google",
    "grpc",
    "pyarrow",
    "pymysql",
    "psycopg",
    "snowflake",
    "sqlalchemy",
    "temporalio",
    "trino",
)


@pytest.mark.parametrize("package", _EXPECTED_IGNORED)
def test_root_conftest_shields_the_package_from_the_freeze_time_sweep(package: str) -> None:
    assert package in freezegun.config.settings.default_ignore_list


def test_the_stock_ignore_entries_are_still_present() -> None:
    """``extend_ignore_list`` must EXTEND freezegun's defaults, not replace them."""
    assert "six.moves" in freezegun.config.settings.default_ignore_list
