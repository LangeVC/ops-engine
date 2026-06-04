"""Tests for CORE-007: MigrationRunner — CREATE INDEX CONCURRENTLY handling.

Postgres rejects CONCURRENTLY inside a transaction. The runner must auto-detect
those statements and run them separately in autocommit mode.
"""
from __future__ import annotations

import pytest

from ops_engine.modules.migration_runner import (
    LocalDirSource,
    MigrationRunner,
    _split_concurrent_indexes,
    _strip_outer_transaction,
)

from tests._migration_runner_fakes import (
    FakeConnection,
    make_connect,
    write_migration,
)


CONCURRENT_BODY = """\
BEGIN;

DO $$ BEGIN
    ALTER TABLE listings ADD COLUMN is_deprecated BOOLEAN DEFAULT FALSE;
EXCEPTION
    WHEN duplicate_column THEN RAISE NOTICE 'is_deprecated already exists';
END $$;

COMMIT;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_listings_is_deprecated
    ON listings (is_deprecated);
"""


def test_strip_outer_transaction_drops_one_pair():
    body = "BEGIN;\nSELECT 1;\nCOMMIT;\n"
    out = _strip_outer_transaction(body)
    assert "BEGIN" not in out
    assert "COMMIT" not in out
    assert "SELECT 1;" in out


def test_strip_outer_transaction_no_op_when_absent():
    body = "SELECT 1;\nSELECT 2;\n"
    assert _strip_outer_transaction(body) == body


def test_strip_outer_transaction_preserves_do_block_keywords():
    """DO $$ BEGIN ... END $$ contains a non-transactional BEGIN — must survive."""
    body = (
        "BEGIN;\n"
        "DO $$ BEGIN\n"
        "    ALTER TABLE x ADD COLUMN y INT;\n"
        "EXCEPTION WHEN duplicate_column THEN RAISE NOTICE 'y exists';\n"
        "END $$;\n"
        "COMMIT;\n"
    )
    out = _strip_outer_transaction(body)
    assert "DO $$ BEGIN" in out
    assert "END $$" in out
    # The outer BEGIN/COMMIT are gone, but the inner DO block's BEGIN survives.
    # We check by counting "BEGIN" tokens: should be just the inner one.
    assert out.count("BEGIN") == 1


def test_split_concurrent_indexes_extracts_top_level():
    tx, concurrent = _split_concurrent_indexes(CONCURRENT_BODY)
    assert "CREATE INDEX CONCURRENTLY" not in tx
    assert len(concurrent) == 1
    assert "CREATE INDEX CONCURRENTLY" in concurrent[0]
    assert "ix_listings_is_deprecated" in concurrent[0]


def test_split_concurrent_indexes_no_op_when_absent():
    body = "ALTER TABLE foo ADD COLUMN bar INT;\n"
    tx, concurrent = _split_concurrent_indexes(body)
    assert tx == body
    assert concurrent == []


def test_apply_runs_concurrent_index_in_autocommit(tmp_path):
    """End-to-end: the CONCURRENTLY stmt must execute outside the transaction.

    Specifically, no BEGIN/COMMIT must wrap the CONCURRENTLY execution, and the
    connection that ran it must have ``autocommit`` flipped on.
    """
    write_migration(tmp_path, "0014_trust_5state.sql", CONCURRENT_BODY)

    conn = FakeConnection()
    runner = MigrationRunner(
        db_url="postgresql://fake",
        source=LocalDirSource(tmp_path),
        connect_fn=make_connect(conn),
        sleep_fn=lambda _s: None,
    )

    result = runner.apply_pending(applied_by="test")
    assert result.errors == []
    assert result.applied == ["0014_trust_5state"]

    # Locate the CREATE INDEX CONCURRENTLY call.
    concurrent_calls = [
        i for i, c in enumerate(conn.calls)
        if "CREATE INDEX CONCURRENTLY" in c.sql
    ]
    assert len(concurrent_calls) == 1
    idx = concurrent_calls[0]

    # All transactional control statements (BEGIN, SET LOCAL, INSERT, COMMIT)
    # must come BEFORE the concurrent index call.
    before = conn.calls[:idx]
    after = conn.calls[idx + 1:]

    # Tracking row insert must already have committed by the time we get here —
    # the autocommit autocomit fake doesn't model real Postgres tx semantics,
    # but we can still verify ordering.
    assert any(c.sql.startswith("INSERT INTO") for c in before)
    assert any("__COMMIT__" in c.sql or "COMMIT" in c.sql for c in before)
    # And after the CONCURRENTLY call there must be no further BEGIN/SET LOCAL
    # (i.e. nothing tried to wrap it in a transaction post-hoc).
    assert not any("SET LOCAL lock_timeout" in c.sql for c in after)

    # The connection's autocommit flag was set at some point.
    assert conn.autocommit is True
