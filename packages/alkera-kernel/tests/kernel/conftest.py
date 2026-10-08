"""Fixtures for the kernel tests live in ``nbkrn_harness`` (a unique module
name, so tests import its helpers without colliding with other conftests).
This conftest sits one level down on purpose: ``packages/alkera-kernel`` is
on ``sys.path`` (editable install), and a ``conftest.py`` in its ``tests`` folder would
be a candidate for other suites' ``from tests.conftest import``.

The kernel speaks over a Unix socket and leads a POSIX process group; v1 has
no Windows kernel (a TCP transport is the seam), so on Windows only the pure
tests are collected."""

import sys

import pytest
from nbkrn_harness import kernel_mount, prebuild_envs, rich_python, start_kernel

__all__ = ["kernel_mount", "rich_python", "start_kernel"]


def pytest_collection_finish(session: pytest.Session) -> None:
    # The cached test environments build here, outside every test's timeout.
    # A whole-suite run loads this conftest, so it covers the notebook and
    # alkera-py tests that use the same environments too.
    prebuild_envs(session)


collect_ignore_glob = (
    [
        "test_nbkrn_[!t]*.py",  # everything that starts a kernel ...
        "test_nbkrn_streams.py",  # ... and the stream tests (fcntl, termios)
    ]
    if sys.platform == "win32"
    else []
)
