"""A name a person types for something they own: never blank, never padded.

``Field(min_length=1)`` counts spaces, so a name of three spaces passed it and
was stored as an empty or all-space label nobody could read or click. A request
schema takes such a name as ``Annotated[str, non_blank_name(limit)]`` instead:
surrounding whitespace is stripped first, and what is left must be one to
``limit`` characters, so a blank name is the ordinary 422 a malformed field
earns and a padded one is stored as it reads.
"""

from __future__ import annotations

from pydantic import StringConstraints


def non_blank_name(max_length: int) -> StringConstraints:
    """The constraint for a name of one to ``max_length`` characters once its
    surrounding whitespace is stripped."""
    return StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length)


__all__ = ["non_blank_name"]
