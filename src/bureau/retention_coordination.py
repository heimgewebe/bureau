"""Cooperative root locks for Bureau writers that affect retention references."""

from __future__ import annotations

import errno
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path


class ReferenceMutationLockError(RuntimeError):
    """Raised when the cooperative retention mutation boundary cannot be established."""


def _open_root(root: Path) -> tuple[int, Path, tuple[int, int]]:
    raw = root.expanduser()
    if raw.is_symlink():
        raise ReferenceMutationLockError(f"reference mutation root is a symlink: {raw}")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise ReferenceMutationLockError(
            f"reference mutation root is unavailable: {raw}"
        ) from exc
    if not resolved.is_dir():
        raise ReferenceMutationLockError(
            f"reference mutation root is not a directory: {resolved}"
        )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise ReferenceMutationLockError(
            f"reference mutation root cannot be opened: {resolved}"
        ) from exc
    metadata = os.fstat(descriptor)
    return descriptor, resolved, (int(metadata.st_dev), int(metadata.st_ino))


@contextmanager
def reference_mutation_lock(
    *roots: Path,
    blocking: bool = False,
) -> Iterator[tuple[Path, ...]]:
    """Serialize cooperating reference writers without permitting silent lock waits."""

    opened: list[tuple[int, Path, tuple[int, int]]] = []
    acquired: list[tuple[int, Path, tuple[int, int]]] = []
    try:
        by_identity: dict[tuple[int, int], tuple[int, Path, tuple[int, int]]] = {}
        for root in roots:
            descriptor, resolved, identity = _open_root(Path(root))
            previous = by_identity.get(identity)
            if previous is not None:
                os.close(descriptor)
                continue
            record = (descriptor, resolved, identity)
            by_identity[identity] = record
            opened.append(record)

        ordered = sorted(opened, key=lambda item: (item[2][0], item[2][1], str(item[1])))
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        for record in ordered:
            descriptor, resolved, _identity = record
            try:
                fcntl.flock(descriptor, operation)
            except OSError as exc:
                if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ReferenceMutationLockError(
                        f"reference mutation root is busy: {resolved}"
                    ) from exc
                raise ReferenceMutationLockError(
                    f"reference mutation root lock failed: {resolved}"
                ) from exc
            acquired.append(record)
        yield tuple(record[1] for record in ordered)
    finally:
        for descriptor, _resolved, _identity in reversed(acquired):
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        for descriptor, _resolved, _identity in reversed(opened):
            with suppress(OSError):
                os.close(descriptor)
