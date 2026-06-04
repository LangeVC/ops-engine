"""Tests for CORE-007: MigrationRunner.apply_pending — happy path."""
from __future__ import annotations

import pytest

from ops_engine.modules.migration_runner import (
    LocalDirSource,
    MigrationRunner,
)

from tests._migration_runner_fakes import (
    FakeConnection,
    make_connect,
    write_migration,
)


@pytest.fixture
def migrations_dir(tmp_path):
    write_migration(tmp_path, "0001_init.sql", "CREATE TABLE foo (id INT);")
    write_migration(tmp_path, "0002_add_bar.sql", "ALTER TABLE foo ADD COLUMN bar TEXT;")
    write_migration(
        tmp_path,
        "0003_with_outer_tx.sql",
        "BEGIN;\nALTER TABLE foo ADD COLUMN baz INT;\nCOMMIT;\n",
    )
    # A non-matching file that should be ignored by the runner.
    (tmp_path / "README.md").write_text("not a migration")
    return tmp_path


def _make_runner(migrations_dir, conn):
    return MigrationRunner(
        db_url="postgresql://fake",
        source=LocalDirSource(migrations_dir),
        connect_fn=make_connect(conn),
        sleep_fn=lambda _s: None,
    )


def test_apply_pending_records_three_files_in_order(migrations_dir):
    conn = FakeConnection()
    runner = _make_runner(migrations_dir, conn)

    result = runner.apply_pending(applied_by="test")

    assert result.errors == []
    assert result.applied == [
        "0001_init",
        "0002_add_bar",
        "0003_with_outer_tx",
    ]
    # Tracking table now has three rows in version order.
    assert [v for v, _ in conn.tracking_rows] == [
        "0001_init",
        "0002_add_bar",
        "0003_with_outer_tx",
    ]


def test_apply_pending_strips_outer_begin_commit(migrations_dir):
    """Files containing their own BEGIN/COMMIT must not double-wrap the tx."""
    conn = FakeConnection()
    runner = _make_runner(migrations_dir, conn)
    runner.apply_pending(applied_by="test")

    # The 0003 file body becomes ``ALTER TABLE foo ADD COLUMN baz INT;`` — no
    # nested BEGIN inside the runner's own transaction.
    body_calls = [
        c for c in conn.calls
        if c.sql.strip().startswith("ALTER TABLE foo ADD COLUMN baz")
    ]
    assert body_calls, "expected at least one ADD COLUMN baz call"
    # And the surrounding sequence had exactly one BEGIN before it.
    idx = conn.calls.index(body_calls[0])
    begins_before = [c for c in conn.calls[:idx] if c.sql.strip() == "BEGIN"]
    # Each of the three files contributes one BEGIN, but no nested ones came
    # from inside 0003's body.
    assert len(begins_before) == 3, [c.sql for c in conn.calls[:idx]]


def test_apply_pending_sets_lock_timeout(migrations_dir):
    conn = FakeConnection()
    runner = _make_runner(migrations_dir, conn)
    runner.apply_pending(applied_by="test")

    lock_calls = [c for c in conn.calls if "SET LOCAL lock_timeout" in c.sql]
    # One per file.
    assert len(lock_calls) == 3
    for c in lock_calls:
        assert "'5s'" in c.sql


def test_apply_pending_records_applied_by(migrations_dir):
    conn = FakeConnection()
    runner = _make_runner(migrations_dir, conn)
    runner.apply_pending(applied_by="backfill-20260604")

    inserts = [c for c in conn.calls if c.sql.startswith("INSERT INTO ")]
    assert len(inserts) == 3
    for c in inserts:
        assert c.params is not None
        assert c.params[2] == "backfill-20260604"


def test_apply_pending_dry_run_does_not_mutate(migrations_dir):
    conn = FakeConnection()
    runner = _make_runner(migrations_dir, conn)

    result = runner.apply_pending(applied_by="test", dry_run=True)

    assert result.dry_run is True
    assert result.applied == ["0001_init", "0002_add_bar", "0003_with_outer_tx"]
    assert conn.tracking_rows == []
    # Connection should never have been used for INSERT/UPDATE.
    assert all(
        not c.sql.startswith("INSERT INTO") for c in conn.calls
    )


def test_check_pending_reports_all_pending_initially(migrations_dir):
    conn = FakeConnection()
    runner = _make_runner(migrations_dir, conn)

    check = runner.check_pending()
    assert check.applied == []
    assert check.pending == ["0001_init", "0002_add_bar", "0003_with_outer_tx"]
    assert check.checksum_mismatches == []
    assert check.has_drift is True


def test_invalid_table_name_rejected():
    with pytest.raises(ValueError, match="invalid table_name"):
        MigrationRunner(
            db_url="postgresql://fake",
            source=LocalDirSource("."),
            table_name="schema_migrations; DROP TABLE users;--",
        )


def test_invalid_lock_timeout_rejected():
    with pytest.raises(ValueError, match="invalid lock_timeout"):
        MigrationRunner(
            db_url="postgresql://fake",
            source=LocalDirSource("."),
            lock_timeout="5s'; DROP TABLE users; --",
        )
