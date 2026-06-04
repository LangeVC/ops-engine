"""Shared fakes for MigrationRunner tests.

Avoids pulling in testcontainers / pytest-postgresql by providing a hand-rolled
psycopg2-compatible connection that records every SQL statement and simulates
just enough behaviour (SELECT/INSERT on the tracking table, autocommit flag)
for the runner's contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class SqlCall:
    sql: str
    params: Optional[tuple]


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self.conn = conn
        self._fetch: list[tuple] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, sql: str, params: Optional[tuple] = None) -> None:
        call = SqlCall(sql=sql, params=params)
        self.conn.calls.append(call)
        # SELECTs against the tracking table return what we've recorded.
        if "SELECT version, checksum" in sql:
            self._fetch = list(self.conn.tracking_rows)
            return
        # Tracking-table INSERTs land in our in-memory state so subsequent
        # apply passes see them.
        if sql.startswith("INSERT INTO ") and "(version, applied_at, checksum, applied_by)" in sql:
            assert params and len(params) == 3, params
            version, checksum, _by = params
            # Real Postgres would PK-conflict on duplicate insert; simulate.
            for v, _c in self.conn.tracking_rows:
                if v == version:
                    raise RuntimeError(
                        f"duplicate key value violates unique constraint "
                        f"(version={version})"
                    )
            self.conn.tracking_rows.append((version, checksum))
            return
        # Optional behaviour hook for individual tests (e.g. failure injection).
        if self.conn.on_execute is not None:
            self.conn.on_execute(call)

    def fetchall(self) -> list[tuple]:
        return self._fetch


@dataclass
class FakeConnection:
    # (version, checksum) rows in the tracking table.
    tracking_rows: list[tuple[str, str]] = field(default_factory=list)
    # Every execute() call across every cursor we've handed out.
    calls: list[SqlCall] = field(default_factory=list)
    # autocommit flag — the runner flips this on for CONCURRENTLY statements.
    autocommit: bool = False
    # Optional callback to inject behaviour (raise on N-th call, etc.).
    on_execute: Optional[Callable[[SqlCall], None]] = None
    committed: bool = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed = True
        self.calls.append(SqlCall(sql="__COMMIT__", params=None))

    def close(self) -> None:
        pass


def make_connect(conn: FakeConnection) -> Callable[[str], FakeConnection]:
    """Return a connect_fn that always returns the same FakeConnection.

    Tests can inspect ``conn.calls`` and ``conn.tracking_rows`` after the run.
    """

    def _connect(_db_url: str) -> FakeConnection:
        return conn

    return _connect


class FakeLockNotAvailable(Exception):
    """Recognised by ``_is_lock_timeout`` via the type-name fallback."""


def write_migration(dirpath, name: str, body: str) -> None:
    """Helper: write a `.sql` file under ``dirpath``."""
    (dirpath / name).write_text(body, encoding="utf-8")
