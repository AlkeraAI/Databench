"""An in-process Alembic run must not silence the loggers that already exist.

``alembic.ini`` carries a ``[loggers]`` section, so ``env.py`` hands it to
``logging.config.fileConfig``. That call's default ``disable_existing_loggers=True``
flips ``disabled`` on every logger created earlier in the process that the ini does
not name -- in a pytest worker, every module-level logger imported so far. The test
that ran Alembic never notices; an unrelated caplog test later in the same process
does, when the record it waits for is never emitted.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alkera_core.config import settings

_BACKEND = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    # The ini path is what routes env.py through fileConfig; a bare Config() skips it.
    config = Config(str(_BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url_sync)
    return config


@pytest.fixture
def pre_existing_logger(caplog: pytest.LogCaptureFixture) -> Iterator[logging.Logger]:
    """A logger that exists BEFORE Alembic runs, wired straight to caplog's handler.

    Direct attachment is deliberate: ``fileConfig`` also detaches every root handler
    (caplog's included) whatever ``disable_existing_loggers`` says, so a record that
    only propagated via root would vanish for an unrelated reason. A pre-existing
    logger keeps its own handlers -- only its ``disabled`` flag is at stake here.
    """
    logger = logging.getLogger(f"backend.tests.alembic_env_logging.{secrets.token_hex(4)}")
    logger.addHandler(caplog.handler)
    try:
        yield logger
    finally:
        logger.removeHandler(caplog.handler)


def test_a_logger_created_before_an_in_process_alembic_run_still_emits_after_it(
    pre_existing_logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=pre_existing_logger.name)

    command.current(_alembic_config())

    assert pre_existing_logger.disabled is False
    pre_existing_logger.warning("still audible after alembic")
    assert [r.getMessage() for r in caplog.records if r.name == pre_existing_logger.name] == [
        "still audible after alembic"
    ]


def test_the_ini_logging_section_is_still_applied() -> None:
    # The fix must not be "stop calling fileConfig": the ini's own levels still land.
    command.current(_alembic_config())

    assert logging.getLogger("alembic").level == logging.INFO
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
