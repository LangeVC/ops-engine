"""Tests for CORE-007: MigrationRunner — lock_timeout retry/backoff loop."""
from __future__ import annotations

import pytest

from ops_engine.modules.migration_runner import (
    LocalDirSource,
    MigrationRunner,
)

from tests._migration_runner_fakes import (
    FakeConnection,
    FakeLockNotAvailable,
    SqlCall,
    make_connect,
    write_migration,
)


@pytest.fixture
def one_migration(tmp_path):
    write_migration(tmp_path, "0001_init.sql", "ALTER TABLE foo ADD COLUMN bar INT;")
    return tmp_path


def test_retries_lock_timeout_then_succeeds(one_migration):
    """Two simulated lock_timeouts, third attempt succeeds. Backoff = [2, 5]."""
    sleeps: list[float] = []

    conn = FakeConnection()
    state = {"failures_left": 2}

    def on_execute(call: SqlCall) -> None:
        # Fail only on the ALTER TABLE statement (the file body). BEGIN, SET
        # LOCAL and INSERT should still go through normally so the eventual
        # success commit looks like a real run.
        if "ALTER TABLE" in call.sql and state["failures_left"] > 0:
            state["failures_left"] -= 1
            raise FakeLockNotAvailable("simulated lock_timeout")

    conn.on_execute = on_execute

    runner = MigrationRunner(
        db_url="postgresql://fake",
        source=LocalDirSource(one_migration),
        connect_fn=make_connect(conn),
        sleep_fn=lambda s: sleeps.append(s),
    )

    result = runner.apply_pending(applied_by="test")

    assert result.errors == []
    assert result.applied == ["0001_init"]
    # Backoff schedule applied for attempts 0 and 1.
    assert sleeps == [2, 5]
    # And the tracking row landed exactly once.
    assert [v for v, _ in conn.tracking_rows] == ["0001_init"]


def test_gives_up_after_max_retries(one_migration):
    """If every attempt hits lock_timeout, ApplyResult.errors is populated."""
    sleeps: list[float] = []
    conn = FakeConnection()

    def on_execute(call: SqlCall) -> None:
        if "ALTER TABLE" in call.sql:
            raise FakeLockNotAvailable("permanent lock_timeout")

    conn.on_execute = on_execute

    runner = MigrationRunner(
        db_url="postgresql://fake",
        source=LocalDirSource(one_migration),
        max_retries=3,
        connect_fn=make_connect(conn),
        sleep_fn=lambda s: sleeps.append(s),
    )

    result = runner.apply_pending(applied_by="test")

    assert result.applied == []
    assert len(result.errors) == 1
    assert "FakeLockNotAvailable" in result.errors[0]
    # Three retries means three backoff sleeps before the fourth (final) try.
    assert sleeps == [2, 5, 10]
    # Tracking table never received the row.
    assert conn.tracking_rows == []


def test_non_transient_error_aborts_immediately(one_migration):
    """Non-lock errors bubble up as an ApplyResult error without retrying."""
    sleeps: list[float] = []
    conn = FakeConnection()

    def on_execute(call: SqlCall) -> None:
        if "ALTER TABLE" in call.sql:
            raise RuntimeError("syntax error at or near 'ALTER'")

    conn.on_execute = on_execute

    runner = MigrationRunner(
        db_url="postgresql://fake",
        source=LocalDirSource(one_migration),
        connect_fn=make_connect(conn),
        sleep_fn=lambda s: sleeps.append(s),
    )

    result = runner.apply_pending(applied_by="test")
    assert result.applied == []
    assert len(result.errors) == 1
    assert "RuntimeError" in result.errors[0]
    # No retries — no backoff sleeps.
    assert sleeps == []


def test_subsequent_files_not_attempted_after_failure(tmp_path):
    """Forward-only: if 0001 fails, 0002 must NOT silently apply."""
    write_migration(tmp_path, "0001_a.sql", "ALTER TABLE foo ADD COLUMN a INT;")
    write_migration(tmp_path, "0002_b.sql", "ALTER TABLE foo ADD COLUMN b INT;")

    conn = FakeConnection()

    def on_execute(call: SqlCall) -> None:
        if "ADD COLUMN a" in call.sql:
            raise RuntimeError("syntax error in 0001")

    conn.on_execute = on_execute

    runner = MigrationRunner(
        db_url="postgresql://fake",
        source=LocalDirSource(tmp_path),
        connect_fn=make_connect(conn),
        sleep_fn=lambda _s: None,
    )

    result = runner.apply_pending(applied_by="test")
    assert result.applied == []
    assert result.errors and "0001_a" in result.errors[0]
    # 0002 must NOT have been touched.
    assert all("ADD COLUMN b" not in c.sql for c in conn.calls)
    assert conn.tracking_rows == []
