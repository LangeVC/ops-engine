"""Tests for CORE-007: MigrationRunner — idempotency on repeated apply."""
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
    return tmp_path


def _make_runner(migrations_dir, conn):
    return MigrationRunner(
        db_url="postgresql://fake",
        source=LocalDirSource(migrations_dir),
        connect_fn=make_connect(conn),
        sleep_fn=lambda _s: None,
    )


def test_apply_twice_is_safe(migrations_dir):
    """Second apply must not error and must not duplicate rows."""
    conn = FakeConnection()
    runner = _make_runner(migrations_dir, conn)

    first = runner.apply_pending(applied_by="first")
    assert first.applied == ["0001_init", "0002_add_bar"]
    assert first.errors == []

    second = runner.apply_pending(applied_by="second")
    # Nothing new to apply; both rows show up as "skipped".
    assert second.applied == []
    assert second.errors == []
    assert sorted(second.skipped) == ["0001_init", "0002_add_bar"]

    # Tracking table still has exactly the original two rows (no dupes).
    versions = [v for v, _ in conn.tracking_rows]
    assert versions == ["0001_init", "0002_add_bar"]


def test_check_pending_after_apply_reports_no_drift(migrations_dir):
    conn = FakeConnection()
    runner = _make_runner(migrations_dir, conn)
    runner.apply_pending(applied_by="test")

    check = runner.check_pending()
    assert check.pending == []
    assert sorted(check.applied) == ["0001_init", "0002_add_bar"]
    assert check.checksum_mismatches == []
    assert check.has_drift is False


def test_partial_apply_resumes_on_next_call(migrations_dir, tmp_path):
    """A fresh file added after a successful apply is picked up next time."""
    conn = FakeConnection()
    runner = _make_runner(migrations_dir, conn)
    runner.apply_pending(applied_by="first")

    write_migration(tmp_path, "0003_add_baz.sql", "ALTER TABLE foo ADD COLUMN baz INT;")

    third = runner.apply_pending(applied_by="second")
    assert third.applied == ["0003_add_baz"]
    assert sorted(third.skipped) == ["0001_init", "0002_add_bar"]
    assert third.errors == []
