

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from typing import Iterator
from uuid import uuid4


class MigrationError(RuntimeError):


_MIGRATION_NAME = re.compile(r"(?P<version>[0-9]{3,})_[a-z0-9_]+\.sql\Z")


def utc_now_sql() -> str:


    return "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


class Database:


    def __init__(self, path: str | Path, *, timeout_seconds: float = 10.0):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self._memory_uri: str | None = None
        self._memory_anchor: sqlite3.Connection | None = None
        if self.path == Path(":memory:"):
            self._memory_uri = (
                f"file:alfa_context_{uuid4().hex}?mode=memory&cache=shared"
            )
            self._memory_anchor = self._open_connection()

    def _open_connection(self) -> sqlite3.Connection:
        if self._memory_uri is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._memory_uri or str(self.path),
            timeout=self.timeout_seconds,
            isolation_level=None,
            uri=self._memory_uri is not None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}"
        )
        if self._memory_uri is None:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def connect(self) -> sqlite3.Connection:
        return self._open_connection()

    def close(self) -> None:
        if self._memory_anchor is not None:
            self._memory_anchor.close()
            self._memory_anchor = None

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(
        self, *, immediate: bool = False
    ) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            else:
                if connection.in_transaction:
                    connection.commit()

    def migrate(self, migrations_dir: str | Path | None = None) -> list[str]:
        directory = (
            Path(migrations_dir)
            if migrations_dir is not None
            else Path(__file__).with_name("migrations")
        )
        if not directory.is_dir():
            raise MigrationError(f"Migration directory not found: {directory}")

        files: list[tuple[str, Path]] = []
        for path in sorted(directory.glob("*.sql")):
            match = _MIGRATION_NAME.fullmatch(path.name)
            if match is None:
                raise MigrationError(f"Invalid migration filename: {path.name}")
            files.append((match.group("version"), path))
        versions = [version for version, _ in files]
        if len(versions) != len(set(versions)):
            raise MigrationError("Migration versions must be unique")

        applied_now: list[str] = []
        with self.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )
            applied = {
                row["version"]: row["checksum"]
                for row in connection.execute(
                    "SELECT version, checksum FROM schema_migrations"
                )
            }
            for version, path in files:
                script = path.read_text(encoding="utf-8")
                checksum = sha256(script.encode("utf-8")).hexdigest()
                previous = applied.get(version)
                if previous is not None:
                    if previous != checksum:
                        raise MigrationError(
                            f"Applied migration {path.name} has changed"
                        )
                    continue
                version_sql = version.replace("'", "''")
                filename_sql = path.name.replace("'", "''")
                checksum_sql = checksum.replace("'", "''")
                batch = (
                    "BEGIN IMMEDIATE;\n"
                    + script
                    + "\nINSERT INTO schema_migrations "
                    "(version, filename, checksum) VALUES "
                    f"('{version_sql}', '{filename_sql}', '{checksum_sql}');\n"
                    "COMMIT;"
                )
                try:
                    connection.executescript(batch)
                except sqlite3.Error as error:
                    if connection.in_transaction:
                        connection.rollback()
                    raise MigrationError(
                        f"Failed to apply migration {path.name}: {error}"
                    ) from error
                applied_now.append(path.name)
                applied[version] = checksum
        return applied_now


__all__ = ["Database", "MigrationError", "utc_now_sql"]
