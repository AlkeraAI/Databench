"""The Files bounds a deployment may tune.

Each of these used to be a module constant somewhere under
``alkera_core.files``, which meant an operator who needed a different figure
had to fork the module. They are settings now, so each one is pinned here three
ways: the default is the figure the code shipped with, the environment moves
it, and a value that is not a bound at all is refused at construction rather
than minting a grant that has already expired.
"""

from __future__ import annotations

import pytest
from alkera_core.config import Settings

#: ``field name -> (default, env var, a value the validator must refuse)``.
BOUNDS: dict[str, tuple[int, str, int]] = {
    "files_content_url_ttl_seconds": (300, "FILES_CONTENT_URL_TTL_SECONDS", 0),
    "files_page_grant_ttl_seconds": (900, "FILES_PAGE_GRANT_TTL_SECONDS", 0),
    "files_archive_url_ttl_seconds": (900, "FILES_ARCHIVE_URL_TTL_SECONDS", -1),
    "files_page_grant_max_requests": (5_000, "FILES_PAGE_GRANT_MAX_REQUESTS", 0),
}


@pytest.mark.parametrize("field", sorted(BOUNDS), ids=sorted(BOUNDS))
def test_the_default_is_the_figure_the_code_shipped_with(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that sets nothing keeps the behaviour it had before."""
    default, env_var, _ = BOUNDS[field]
    monkeypatch.delenv(env_var, raising=False)

    assert getattr(Settings(_env_file=None), field) == default


@pytest.mark.parametrize("field", sorted(BOUNDS), ids=sorted(BOUNDS))
def test_the_environment_moves_the_bound(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var is the one an operator reads back, under the usual naming."""
    default, env_var, _ = BOUNDS[field]
    wanted = default * 3 + 7
    monkeypatch.setenv(env_var, str(wanted))

    assert getattr(Settings(_env_file=None), field) == wanted


@pytest.mark.parametrize("field", sorted(BOUNDS), ids=sorted(BOUNDS))
def test_a_value_that_is_not_a_bound_is_refused(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero or less is not a wider bound; it is one nothing can satisfy.

    Refusing at construction is what keeps the mistake from becoming a grant
    that expires before it is handed over.
    """
    _, env_var, nonsense = BOUNDS[field]
    monkeypatch.setenv(env_var, str(nonsense))

    with pytest.raises(ValueError, match=env_var):
        Settings(_env_file=None)
