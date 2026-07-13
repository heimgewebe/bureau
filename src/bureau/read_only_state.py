"""Read-only projection of Bureau operational state without filesystem effects."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import legacy
from .v2 import StateStore


class ReadOnlyStateStore(StateStore):
    """Expose StateStore reads without migrations or persistent initialization."""

    def __init__(self, path: Path | None = None, state_root: Path | None = None):
        resolved_path = path.expanduser().resolve() if path is not None else None
        if state_root is not None:
            root = state_root.expanduser().resolve()
            if resolved_path is not None and resolved_path.parent != root:
                raise legacy.StateError("state database must be inside state_root")
        elif resolved_path is not None:
            root = resolved_path.parent
        else:
            root = legacy.default_state_dir()
        self.state_root = root
        self.path = resolved_path if resolved_path is not None else root / "bureau.sqlite3"
        self.envelopes_dir = root / "envelopes"
        self.receipts_dir = root / "receipts"
        self.read_only = True
        self._memory_uri: str | None = None
        self._memory_keeper: sqlite3.Connection | None = None
        self._initializing_memory = False
        if not self.path.exists():
            self._memory_uri = f"file:bureau-readonly-{id(self)}?mode=memory&cache=shared"
            self._memory_keeper = sqlite3.connect(
                self._memory_uri,
                uri=True,
                timeout=30,
                isolation_level=None,
            )
            self._initializing_memory = True
            try:
                StateStore._initialize(self)
            finally:
                self._initializing_memory = False

    @staticmethod
    def _configure(connection: sqlite3.Connection, *, query_only: bool) -> sqlite3.Connection:
        connection.row_factory = sqlite3.Row
        if query_only:
            connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def connect(self) -> sqlite3.Connection:
        if self._memory_uri is not None:
            connection = sqlite3.connect(
                self._memory_uri,
                uri=True,
                timeout=30,
                isolation_level=None,
            )
            return self._configure(
                connection,
                query_only=not self._initializing_memory,
            )
        connection = sqlite3.connect(
            f"file:{self.path}?mode=ro",
            uri=True,
            timeout=30,
            isolation_level=None,
        )
        return self._configure(connection, query_only=True)

    def __del__(self) -> None:
        keeper = getattr(self, "_memory_keeper", None)
        if keeper is not None:
            keeper.close()
