"""The errors ``alkera`` raises, importable without the rest of the package."""

from __future__ import annotations


class ConnectionNotConfigured(LookupError):  # noqa: N818 - the public name is the outcome
    """``alkera.sql`` named a connection this environment has no
    configuration for."""


class ServiceUnavailable(RuntimeError):  # noqa: N818 - the public name is the outcome
    """``alkera.call`` ran where no Alkera service is reachable (plain
    Python, Jupyter or stock marimo)."""


class StopCell(Exception):  # noqa: N818 - the public name is the outcome
    """Raised by ``alkera.stop`` outside the Alkera kernel. The kernel and
    stock marimo raise their own stop exception instead, so the cell ends
    as stopped rather than failed."""

    _alkera_stop = True


class FeatureMissing(ImportError):  # noqa: N818 - the public name is the outcome
    """A part of the public API that this installation does not ship."""


__all__ = ["ConnectionNotConfigured", "FeatureMissing", "ServiceUnavailable", "StopCell"]
