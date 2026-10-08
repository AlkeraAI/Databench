"""The Files library's third-party dependencies are installed, functional and locked.

Every later Files lane builds on these: BLAKE3 content hashes, the S3-compatible
store driver, property tests over names and trees, and the coverage gate. A missing
wheel or an unlocked package would otherwise surface as an unrelated collection error
several lanes downstream, so it is pinned here instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The published BLAKE3 digest of the empty input — proves the wheel computes real
# BLAKE3 rather than merely importing.
EMPTY_BLAKE3 = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"

LOCKED_PACKAGES = ("blake3", "hypothesis", "pytest-cov", "aioboto3")


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "uv.lock").is_file():
            return candidate
    raise AssertionError("uv.lock not found above the test file")


def test_blake3_is_installed_and_hashes_correctly() -> None:
    import blake3

    assert blake3.blake3(b"").hexdigest() == EMPTY_BLAKE3


def test_hypothesis_is_installed() -> None:
    import hypothesis

    assert hypothesis.given is not None


def test_boto3_and_aioboto3_are_installed() -> None:
    import aioboto3
    import boto3
    import botocore

    assert boto3.session is not None
    assert botocore.__version__
    assert aioboto3.Session is not None


def test_pytest_cov_is_installed() -> None:
    import pytest_cov

    assert pytest_cov.__version__


@pytest.mark.parametrize("package", LOCKED_PACKAGES)
def test_package_is_present_in_the_repo_lockfile(package: str) -> None:
    lock = (_repo_root() / "uv.lock").read_text(encoding="utf-8")
    assert f'name = "{package}"' in lock
