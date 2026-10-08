"""``alkera.persistent_cache``: a function's results kept on disk across
kernel restarts.

Results are pickled under ``__marimo__/cache/alkera/`` in the notebook's
directory (the working directory when the call runs), one file per call,
named by the ``sha256`` of the function's source and its pickled arguments.
Changing the function's code or its arguments misses the cache; an entry
that cannot be read (truncated, corrupt, from an incompatible library) is
recomputed and rewritten. Writes are atomic, so a crash mid-write never
leaves a partial entry.

The entries are pickles: anyone who can write the notebook's directory can
make loading one run code. Keep the cache inside directories you trust,
which is where it is by default.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import marshal
import os
import pickle
import re
import tempfile
import typing
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast, overload

__all__ = ["CACHE_SUBDIR", "persistent_cache"]

CACHE_SUBDIR = Path("__marimo__") / "cache" / "alkera"
_MAGIC = "alkera.persistent_cache"
_VERSION = 1
_PICKLE_PROTOCOL = 4

F = TypeVar("F", bound=typing.Callable[..., Any])


def _source_of(fn: Callable[..., Any]) -> bytes:
    try:
        return inspect.getsource(fn).encode("utf-8")
    except (OSError, TypeError):
        code = getattr(fn, "__code__", None)
        if code is None:
            return repr(fn).encode("utf-8")
        return marshal.dumps(code)


def cache_key(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """The entry's name for one call. Raises ``pickle.PicklingError`` (or
    ``TypeError``) when an argument cannot be pickled."""
    digest = hashlib.sha256()
    digest.update(f"{_MAGIC}:{_VERSION}\0".encode())
    digest.update(getattr(fn, "__qualname__", "").encode("utf-8") + b"\0")
    digest.update(_source_of(fn) + b"\0")
    digest.update(pickle.dumps((args, sorted(kwargs.items())), protocol=_PICKLE_PROTOCOL))
    return digest.hexdigest()


def _entry_path(directory: Path, fn: Callable[..., Any], key: str) -> Path:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", getattr(fn, "__qualname__", "fn"))[:80]
    return directory / f"{name}-{key}.pkl"


_MISS = object()


def _load(path: Path) -> Any:
    try:
        with path.open("rb") as handle:
            record = pickle.load(handle)  # noqa: S301 - the notebook's own cache directory
    except FileNotFoundError:
        return _MISS
    except Exception:
        return _MISS  # truncated or corrupt: recompute
    if (
        isinstance(record, tuple)
        and len(record) == 3
        and record[0] == _MAGIC
        and record[1] == _VERSION
    ):
        return record[2]
    return _MISS


def _store(path: Path, value: Any) -> None:
    payload = pickle.dumps((_MAGIC, _VERSION, value), protocol=_PICKLE_PROTOCOL)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _decorate(fn: F, directory: str | os.PathLike[str] | None) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        root = Path(directory) if directory is not None else Path.cwd() / CACHE_SUBDIR
        try:
            key = cache_key(fn, args, kwargs)
        except Exception as exc:
            warnings.warn(
                f"alkera.persistent_cache: {fn.__qualname__} was called with an argument that "
                f"cannot be pickled ({exc}); running it without the cache",
                stacklevel=2,
            )
            return fn(*args, **kwargs)
        path = _entry_path(root, fn, key)
        cached = _load(path)
        if cached is not _MISS:
            return cached
        value = fn(*args, **kwargs)
        try:
            _store(path, value)
        except (pickle.PicklingError, TypeError, AttributeError) as exc:
            warnings.warn(
                f"alkera.persistent_cache: the result of {fn.__qualname__} cannot be pickled "
                f"({exc}); not cached",
                stacklevel=2,
            )
        return value

    return cast(F, wrapper)


@overload
def persistent_cache(fn: F, /) -> F: ...


@overload
def persistent_cache(*, directory: str | os.PathLike[str] | None = None) -> Callable[[F], F]: ...


def persistent_cache(
    fn: F | None = None, /, *, directory: str | os.PathLike[str] | None = None
) -> F | Callable[[F], F]:
    """Cache ``fn``'s results on disk, keyed by its source and arguments.

    ``@alkera.persistent_cache`` or ``@alkera.persistent_cache(directory=...)``.
    """
    if fn is not None:
        return _decorate(fn, directory)

    def decorator(inner: F) -> F:
        return _decorate(inner, directory)

    return decorator
