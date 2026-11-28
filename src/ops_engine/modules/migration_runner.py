"""CORE-007: MigrationRunner — track and apply SQL migrations.

Replaces the "``psql -f`` during deploy" anti-pattern: a tracking table records
which migrations have been applied, drift can be detected by cron, and applies
go through one controlled transaction per file with ``SET LOCAL lock_timeout``
and a backoff retry loop. ``CREATE INDEX CONCURRENTLY`` statements are
auto-detected and run outside the transaction (Postgres requirement).

Forward-only — there is intentionally **no** downgrade / rollback. Fix forward.

Generic over the target database. The canonical caller is an org layover (see
`capacium-ops`) that initializes one ``MigrationRunner`` per service and wires
it into a cron drift-check + an admin-token-protected apply endpoint.

YAML config lives under the org block; see the runtime example in the README.

Optional runtime dep: ``psycopg2``. Install with ``pip install ops-engine[postgres]``.
"""
from __future__ import annotations

import abc
import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

# ── Lazy optional dep ────────────────────────────────────────────────────────
# psycopg2 is only needed at apply-time. Importing it lazily lets the rest of
# ops-engine import cleanly without it, and lets unit tests inject a fake
# ``connect_fn`` without installing the driver.
try:
    import psycopg2  # type: ignore[import-not-found]
    from psycopg2 import errors as psycopg2_errors  # type: ignore[import-not-found]
    _HAVE_PSYCOPG2 = True
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]
    psycopg2_errors = None  # type: ignore[assignment]
    _HAVE_PSYCOPG2 = False


# ── Data classes ─────────────────────────────────────────────────────────────

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.sql$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class MigrationFile:
    """One on-disk migration file."""
    version: str   # filename without ``.sql`` (e.g. ``0012_add_triggers_pricing``)
    path: Path
    checksum: str  # sha256 hex over file bytes


@dataclass
class CheckResult:
    """Outcome of a read-only drift check."""
    applied: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    # Each entry is a version string. The runner reports two flavours of drift:
    #   1. ``<version>`` — file in source has a checksum that differs from the
    #      recorded one (someone hand-edited a historical migration).
    #   2. ``<version> (in DB, missing in source)`` — a row in the tracking
    #      table has no matching file (someone applied something not in source).
    checksum_mismatches: list[str] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.pending or self.checksum_mismatches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": list(self.applied),
            "pending": list(self.pending),
            "checksum_mismatches": list(self.checksum_mismatches),
            "has_drift": self.has_drift,
        }


@dataclass
class ApplyResult:
    """Outcome of an apply pass."""
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # already in tracking table
    dry_run: bool = False
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": list(self.applied),
            "skipped": list(self.skipped),
            "dry_run": self.dry_run,
            "duration_ms": self.duration_ms,
            "errors": list(self.errors),
            "ok": self.ok,
        }


# ── Sources ──────────────────────────────────────────────────────────────────

class MigrationSource(abc.ABC):
    """A source of ``.sql`` migration files.

    ``materialize`` is a context manager that yields a local filesystem path
    containing the ``[0-9]{4}_*.sql`` files. Implementations may clone, copy,
    or no-op; the runner does not care.
    """

    @abc.abstractmethod
    def materialize(self) -> "contextlib.AbstractContextManager[Path]":
        ...


class LocalDirSource(MigrationSource):
    """For tests + dev. Points at an existing directory of ``.sql`` files."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.is_dir():
            raise ValueError(f"LocalDirSource: not a directory: {self.path}")

    @contextlib.contextmanager
    def materialize(self) -> Iterator[Path]:
        yield self.path


class GitRepoSource(MigrationSource):
    """Shallow ``git clone --depth 1`` into a temp dir.

    Cleans up the temp dir on exit. If ``token_env_var`` is set and the URL is a
    ``https://github.com/…`` URL, the runner rewrites it to
    ``https://x-access-token:<token>@github.com/…`` for the clone. Falls back to
    anonymous clone if the env var is unset (with a WARNING log).
    """

    def __init__(
        self,
        url: str,
        *,
        ref: str = "main",
        subpath: str = "migrations",
        token_env_var: Optional[str] = None,
    ):
        self.url = url
        self.ref = ref
        self.subpath = subpath
        self.token_env_var = token_env_var

    def _authenticated_url(self) -> str:
        if not self.token_env_var:
            return self.url
        token = os.environ.get(self.token_env_var)
        if not token:
            logger.warning(
                "GitRepoSource: %s is not set; cloning anonymously",
                self.token_env_var,
            )
            return self.url
        if self.url.startswith("https://github.com/"):
            return self.url.replace(
                "https://github.com/",
                f"https://x-access-token:{token}@github.com/",
                1,
            )
        if "${TOKEN}" in self.url:
            return self.url.replace("${TOKEN}", token)
        return self.url

    @contextlib.contextmanager
    def materialize(self) -> Iterator[Path]:
        tmpdir = Path(tempfile.mkdtemp(prefix="ops-engine-migrations-"))
        try:
            cmd = [
                "git", "clone", "--depth", "1",
                "--branch", self.ref,
                self._authenticated_url(),
                str(tmpdir / "repo"),
            ]
            logger.info("GitRepoSource: cloning %s @ %s", self.url, self.ref)
            try:
                subprocess.run(cmd, check=True, capture_output=True)
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
                raise RuntimeError(
                    f"git clone failed for {self.url}@{self.ref}: {stderr.strip()}"
                ) from e
            sub = tmpdir / "repo" / self.subpath
            if not sub.is_dir():
                raise RuntimeError(
                    f"GitRepoSource: subpath {self.subpath!r} does not exist in "
                    f"{self.url}@{self.ref}"
                )
            yield sub
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── SQL parsing helpers ──────────────────────────────────────────────────────

# Top-level CREATE INDEX CONCURRENTLY (case-insensitive, after comment strip).
_CONCURRENTLY_RE = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY",
    re.IGNORECASE | re.MULTILINE,
)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_DOLLAR_QUOTE_RE = re.compile(r"\$\$")


def _strip_line_comments(sql: str) -> str:
    return _LINE_COMMENT_RE.sub("", sql)


def _checksum_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _checksum_file(path: Path) -> str:
    return _checksum_bytes(path.read_bytes())


def _strip_outer_transaction(sql: str) -> str:
    """Remove a single outer ``BEGIN;`` … ``COMMIT;`` pair if present.

    The runner provides its own transaction wrapper (with SET LOCAL
    lock_timeout). Files may or may not include their own BEGIN/COMMIT; this
    normalises both shapes. Only the *outermost* pair is stripped — nested
    BEGIN/COMMIT inside ``DO $$ … $$`` blocks are left alone (they aren't
    transaction-control statements there anyway).
    """
    pattern_begin = re.compile(
        r"\A(?:\s*(?:--[^\n]*\n|\s))*BEGIN\s*;\s*", re.IGNORECASE
    )
    pattern_commit = re.compile(
        r"\s*COMMIT\s*;\s*(?:\s*(?:--[^\n]*\n|\s))*\Z", re.IGNORECASE
    )
    m_begin = pattern_begin.match(sql)
    m_commit = pattern_commit.search(sql)
    if m_begin and m_commit and m_commit.start() >= m_begin.end():
        return sql[m_begin.end():m_commit.start()] + "\n"
    return sql


def _split_concurrent_indexes(sql: str) -> tuple[str, list[str]]:
    """Return ``(transactional_sql, [concurrent_index_stmts])``.

    Splits the input at top-level ``;`` boundaries (tracking ``$$ … $$`` so we
    don't break ``DO $$ … END $$`` blocks). Any statement starting with
    ``CREATE [UNIQUE] INDEX CONCURRENTLY`` (after comments) is pulled out into
    the second list, to be run outside any transaction.
    """
    cleaned = _strip_line_comments(sql)
    if not _CONCURRENTLY_RE.search(cleaned):
        return sql, []

    statements: list[str] = []
    concurrent: list[str] = []
    buf: list[str] = []
    in_dollar = False
    for line in sql.splitlines(keepends=True):
        # Every $$ toggles dollar-quote state.
        for _ in _DOLLAR_QUOTE_RE.findall(line):
            in_dollar = not in_dollar
        buf.append(line)
        if not in_dollar and line.rstrip().endswith(";"):
            stmt = "".join(buf)
            buf = []
            head = _strip_line_comments(stmt).strip()
            if _CONCURRENTLY_RE.match(head):
                concurrent.append(stmt.strip())
            else:
                statements.append(stmt)
    if buf:
        statements.append("".join(buf))
    return "".join(statements), concurrent


# ── Notification hook ────────────────────────────────────────────────────────

# Same shape as health_monitor sinks: a callable receiving an event dict.
# The layover passes one of these in to route apply/drift events to its
# existing notification channels.
NotificationHook = Callable[[dict[str, Any]], None]


def _default_notifier(event: dict[str, Any]) -> None:
    logger.info("migration_runner notify: %s", event)


# ── Connection abstraction (for tests) ───────────────────────────────────────

# A ``connect_fn(db_url) -> psycopg2-style connection``. Tests inject a fake;
# prod uses ``psycopg2.connect`` via ``_default_connect``.
Connection = Any
ConnectFn = Callable[[str], Connection]


def _default_connect(db_url: str) -> Connection:
    if not _HAVE_PSYCOPG2:
        raise RuntimeError(
            "psycopg2 is not installed. "
            "Install with: pip install 'ops-engine[postgres]'"
        )
    return psycopg2.connect(db_url)


def _is_lock_timeout(exc: BaseException) -> bool:
    """Best-effort detection of Postgres ``lock_timeout`` errors (SQLSTATE 55P03).

    Works against real ``psycopg2.errors.LockNotAvailable``, anything carrying a
    ``pgcode`` attribute of ``'55P03'``, and test fakes named ``LockNotAvailable``
    or ``FakeLockNotAvailable``.
    """
    if _HAVE_PSYCOPG2 and psycopg2_errors is not None:
        if isinstance(exc, psycopg2_errors.LockNotAvailable):
            return True
    if getattr(exc, "pgcode", None) == "55P03":
        return True
    name = type(exc).__name__
    return name in ("LockNotAvailable", "FakeLockNotAvailable")


# ── MigrationRunner ──────────────────────────────────────────────────────────

class MigrationRunner:
    """Track and apply forward-only SQL migrations against a Postgres DB.

    Conventions:

      - Migration files are named ``[0-9]{4}_<slug>.sql`` and sorted lex.
      - One transaction per file, prefixed with ``SET LOCAL lock_timeout``.
        Any ``BEGIN``/``COMMIT`` already present in the file is stripped first.
      - ``CREATE INDEX CONCURRENTLY`` statements are auto-detected and run
        outside the transaction (Postgres requirement) in autocommit mode.
      - Tracking table (default: ``schema_migrations``) stores
        ``(version, applied_at, checksum, applied_by)``.
      - Lock-timeout retries use a fixed backoff schedule:
        ``2s, 5s, 10s, 20s, 30s``, capped at ``max_retries`` attempts.
      - The notifier hook receives one dict per applied / failed file, mirroring
        the shape used by HealthMonitor sinks. The layover routes these into
        the existing notification channels.
    """

    _LOCK_BACKOFF = (2, 5, 10, 20, 30)

    def __init__(
        self,
        *,
        db_url: str,
        source: MigrationSource,
        table_name: str = "schema_migrations",
        lock_timeout: str = "5s",
        max_retries: int = 5,
        applied_by_default: str = "ops-engine",
        notifier: Optional[NotificationHook] = None,
        connect_fn: Optional[ConnectFn] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        if not _IDENT_RE.fullmatch(table_name):
            raise ValueError(
                f"invalid table_name {table_name!r}; must match {_IDENT_RE.pattern}"
            )
        # Also guard lock_timeout — we interpolate it into SQL, so it must look
        # like a Postgres interval literal, not anything user-controlled.
        if not re.fullmatch(r"[0-9]+(?:ms|s|min)?", lock_timeout):
            raise ValueError(
                f"invalid lock_timeout {lock_timeout!r}; "
                "must be a Postgres interval literal like '5s' or '500ms'"
            )
        self.db_url = db_url
        self.source = source
        self.table_name = table_name
        self.lock_timeout = lock_timeout
        self.max_retries = max_retries
        self.applied_by_default = applied_by_default
        self.notifier = notifier or _default_notifier
        self._connect_fn = connect_fn or _default_connect
        self._sleep = sleep_fn

    # -- public API ----------------------------------------------------------

    def ensure_tracking_table(self) -> None:
        """Idempotently create the tracking table."""
        sql = (
            f"CREATE TABLE IF NOT EXISTS {self.table_name} ("
            "  version VARCHAR(255) PRIMARY KEY,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "  checksum TEXT NOT NULL,"
            "  applied_by TEXT NOT NULL DEFAULT 'ops-engine'"
            ");"
        )
        with self._connect_fn(self.db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        logger.info("ensured tracking table %s", self.table_name)

    def list_files(self) -> list[MigrationFile]:
        """Enumerate ``.sql`` files in the source, sorted by version."""
        with self.source.materialize() as base:
            files: list[MigrationFile] = []
            for p in sorted(Path(base).iterdir()):
                if not _MIGRATION_FILENAME_RE.match(p.name):
                    continue
                files.append(MigrationFile(
                    version=p.stem,
                    path=p,
                    checksum=_checksum_file(p),
                ))
            return sorted(files, key=lambda f: f.version)

    def check_pending(self) -> CheckResult:
        """Compare source files vs the tracking table. Read-only; cron-safe."""
        files = self.list_files()
        applied_map = self._fetch_applied()
        result = CheckResult()
        for f in files:
            if f.version in applied_map:
                result.applied.append(f.version)
                if applied_map[f.version] != f.checksum:
                    result.checksum_mismatches.append(f.version)
            else:
                result.pending.append(f.version)
        # Catch rows in the tracking table that have no matching file (someone
        # applied something out-of-band that isn't in the source repo).
        file_versions = {f.version for f in files}
        for v in applied_map:
            if v not in file_versions:
                result.checksum_mismatches.append(
                    f"{v} (in DB, missing in source)"
                )
        return result

    def verify_checksums(self) -> list[str]:
        """Return the list of versions where file checksum != DB checksum.

        Empty result means the tracking table and source are in sync (modulo
        pending files). Non-empty means somebody hand-edited a historical
        migration on disk — callers should hard-fail at startup.
        """
        return self.check_pending().checksum_mismatches

    def apply_pending(
        self,
        *,
        applied_by: Optional[str] = None,
        dry_run: bool = False,
    ) -> ApplyResult:
        """Apply each pending file in order, one transaction per file.

        Stops on the first non-transient failure (lock_timeout is transient and
        retried). On stop, ``ApplyResult.errors`` is non-empty and remaining
        pending files are NOT silently skipped — they stay pending until the
        next call.
        """
        started = time.time()
        applied_by = applied_by or self.applied_by_default
        result = ApplyResult(dry_run=dry_run)

        # Materialise once — for a GitRepoSource this avoids cloning N times
        # (once per file). list_files() re-materialises; here we do it
        # explicitly so list + apply share the same checkout.
        with self.source.materialize() as base:
            files = [
                MigrationFile(
                    version=p.stem,
                    path=p,
                    checksum=_checksum_file(p),
                )
                for p in sorted(Path(base).iterdir())
                if _MIGRATION_FILENAME_RE.match(p.name)
            ]
            applied_map = self._fetch_applied()
            pending = [f for f in files if f.version not in applied_map]
            result.skipped = [
                f.version for f in files if f.version in applied_map
            ]
            if not pending:
                result.duration_ms = int((time.time() - started) * 1000)
                return result
            for f in pending:
                if dry_run:
                    logger.info("[dry-run] would apply %s", f.version)
                    result.applied.append(f.version)
                    continue
                try:
                    self._apply_one(f, applied_by=applied_by)
                    result.applied.append(f.version)
                    self.notifier({
                        "event": "migration_applied",
                        "version": f.version,
                        "applied_by": applied_by,
                    })
                except Exception as e:  # noqa: BLE001 — surface in ApplyResult
                    msg = f"{f.version}: {type(e).__name__}: {e}"
                    result.errors.append(msg)
                    self.notifier({
                        "event": "migration_failed",
                        "version": f.version,
                        "error": msg,
                    })
                    logger.error("migration %s failed: %s", f.version, e)
                    # Forward-only — stop the run rather than skip a hole.
                    break
        result.duration_ms = int((time.time() - started) * 1000)
        return result

    # -- internals -----------------------------------------------------------

    def _fetch_applied(self) -> dict[str, str]:
        sql = f"SELECT version, checksum FROM {self.table_name}"
        with self._connect_fn(self.db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        return {r[0]: r[1] for r in rows}

    def _apply_one(self, f: MigrationFile, *, applied_by: str) -> None:
        body = f.path.read_text(encoding="utf-8")
        body = _strip_outer_transaction(body)
        tx_sql, concurrent = _split_concurrent_indexes(body)

        # Transactional portion with lock-timeout + backoff retry.
        for attempt in range(self.max_retries + 1):
            try:
                with self._connect_fn(self.db_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute("BEGIN")
                        cur.execute(
                            f"SET LOCAL lock_timeout = '{self.lock_timeout}'"
                        )
                        if tx_sql.strip():
                            cur.execute(tx_sql)
                        cur.execute(
                            f"INSERT INTO {self.table_name} "
                            "(version, applied_at, checksum, applied_by) "
                            "VALUES (%s, NOW(), %s, %s)",
                            (f.version, f.checksum, applied_by),
                        )
                        cur.execute("COMMIT")
                break
            except Exception as e:  # noqa: BLE001
                if _is_lock_timeout(e) and attempt < self.max_retries:
                    delay = self._LOCK_BACKOFF[
                        min(attempt, len(self._LOCK_BACKOFF) - 1)
                    ]
                    logger.warning(
                        "migration %s: lock_timeout (attempt %d/%d); "
                        "retrying in %ds",
                        f.version, attempt + 1, self.max_retries, delay,
                    )
                    self._sleep(delay)
                    continue
                raise

        # CREATE INDEX CONCURRENTLY — outside any transaction.
        for stmt in concurrent:
            with self._connect_fn(self.db_url) as conn:
                # psycopg2 starts in transaction mode by default; concurrent
                # indexes require autocommit.
                try:
                    conn.autocommit = True
                except AttributeError:
                    pass
                with conn.cursor() as cur:
                    cur.execute(stmt)


# ── Layover helpers ──────────────────────────────────────────────────────────

def runner_from_config(
    name: str,
    cfg: Any,  # ops_engine.config_loader.MigrationTargetConfig
    *,
    notifier: Optional[NotificationHook] = None,
) -> MigrationRunner:
    """Build a ``MigrationRunner`` from a ``MigrationTargetConfig`` instance.

    Resolves the DB URL via ``cfg.db_url_env`` and builds the source from
    ``cfg.source``. Raises ``RuntimeError`` if the env var is unset.
    """
    db_url = os.environ.get(cfg.db_url_env)
    if not db_url:
        raise RuntimeError(
            f"migrations[{name}]: env var {cfg.db_url_env} is not set"
        )
    src_cfg = cfg.source
    if src_cfg.type == "local":
        if not src_cfg.path:
            raise ValueError(
                f"migrations[{name}]: source.type=local requires source.path"
            )
        source: MigrationSource = LocalDirSource(src_cfg.path)
    elif src_cfg.type == "git":
        if not src_cfg.url:
            raise ValueError(
                f"migrations[{name}]: source.type=git requires source.url"
            )
        source = GitRepoSource(
            src_cfg.url,
            ref=src_cfg.ref,
            subpath=src_cfg.subpath,
            token_env_var=src_cfg.token_env_var,
        )
    else:
        raise ValueError(
            f"migrations[{name}]: unknown source.type {src_cfg.type!r}"
        )
    return MigrationRunner(
        db_url=db_url,
        source=source,
        table_name=cfg.table_name,
        lock_timeout=cfg.lock_timeout,
        max_retries=cfg.max_retries,
        applied_by_default=f"ops-engine/{name}",
        notifier=notifier,
    )


__all__ = [
    "MigrationRunner",
    "MigrationSource",
    "LocalDirSource",
    "GitRepoSource",
    "MigrationFile",
    "CheckResult",
    "ApplyResult",
    "NotificationHook",
    "runner_from_config",
]
