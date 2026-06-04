"""Tests for CORE-007: MigrationRunner.verify_checksums."""
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


def _make_runner(migrations_dir, conn):
    return MigrationRunner(
        db_url="postgresql://fake",
        source=LocalDirSource(migrations_dir),
        connect_fn=make_connect(conn),
        sleep_fn=lambda _s: None,
    )


def test_verify_checksums_detects_edited_file(tmp_path):
    """Applying then editing a historical migration must flag a mismatch."""
    write_migration(tmp_path, "0001_init.sql", "CREATE TABLE foo (id INT);")
    conn = FakeConnection()
    runner = _make_runner(tmp_path, conn)

    runner.apply_pending(applied_by="test")
    assert runner.verify_checksums() == []

    # Operator-error: hand-edit a migration that's already been applied.
    write_migration(tmp_path, "0001_init.sql", "CREATE TABLE foo (id BIGINT);")
    mismatches = runner.verify_checksums()
    assert mismatches == ["0001_init"]


def test_verify_checksums_flags_db_only_rows(tmp_path):
    """A row in the tracking table that has no on-disk file is drift too."""
    write_migration(tmp_path, "0001_init.sql", "CREATE TABLE foo (id INT);")
    conn = FakeConnection(
        tracking_rows=[
            ("0001_init", "doesnt-matter"),
            ("9999_phantom", "ghost-checksum"),
        ],
    )
    runner = _make_runner(tmp_path, conn)
    mismatches = runner.verify_checksums()

    # Both: the file checksum doesn't match (we stored "doesnt-matter") and
    # 9999_phantom has no file.
    assert "0001_init" in mismatches
    assert any("9999_phantom" in m and "missing in source" in m for m in mismatches)


def test_check_pending_partitions_state(tmp_path):
    write_migration(tmp_path, "0001_init.sql", "CREATE TABLE foo (id INT);")
    write_migration(tmp_path, "0002_add_bar.sql", "ALTER TABLE foo ADD COLUMN bar TEXT;")

    conn = FakeConnection()
    runner = _make_runner(tmp_path, conn)
    # Apply only 0001 by hand-seeding the tracking table at the correct
    # checksum, then check.
    files = runner.list_files()
    init = next(f for f in files if f.version == "0001_init")
    conn.tracking_rows.append((init.version, init.checksum))

    check = runner.check_pending()
    assert check.applied == ["0001_init"]
    assert check.pending == ["0002_add_bar"]
    assert check.checksum_mismatches == []
    assert check.has_drift is True


@pytest.mark.parametrize("filename", [
    "init.sql",          # no leading version number
    "001_short.sql",     # only 3 digits
    "12345_long.sql",    # 5 digits
    "0001_init.txt",     # wrong extension
])
def test_list_files_ignores_non_conforming_names(tmp_path, filename):
    write_migration(tmp_path, "0001_real.sql", "SELECT 1;")
    write_migration(tmp_path, filename, "SELECT 2;")
    conn = FakeConnection()
    runner = _make_runner(tmp_path, conn)
    versions = [f.version for f in runner.list_files()]
    assert versions == ["0001_real"]
