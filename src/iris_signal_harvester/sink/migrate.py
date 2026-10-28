"""Minimal plain-SQL migration runner (no Alembic).

Applies ``migrations/NNN_name.sql`` files in lexical order, each in its own
transaction, recording ``(version, checksum)`` in ``<schema>.schema_migration``.
A previously applied file whose contents have since changed is an error, not a
silent re-run: schema history must be append-only.

Usage:  IRIS_DB_DSN=... python -m iris_signal_harvester.sink.migrate
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from psycopg import sql

from iris_signal_harvester.config import get_settings
from iris_signal_harvester.errors import ConfigurationError
from iris_signal_harvester.sink._common import DEFAULT_SCHEMA, connect, table

__all__ = ["apply_migrations", "default_migrations_dir"]

# Arbitrary constant: serialises concurrent runners.
_ADVISORY_LOCK_KEY = 0x1815_5160


def default_migrations_dir() -> Path:
    """``<repo>/migrations`` for a source checkout (src/iris_signal_harvester/sink/…)."""
    return Path(__file__).resolve().parents[3] / "migrations"


def apply_migrations(
    dsn: str,
    migrations_dir: Path | None = None,
    *,
    schema: str = DEFAULT_SCHEMA,
) -> list[str]:
    """Apply pending migrations; return the versions applied by this call."""
    directory = migrations_dir or default_migrations_dir()
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise ConfigurationError(f"no migrations found in {directory}")

    applied: list[str] = []
    with connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("SELECT pg_advisory_lock(%s)"), (_ADVISORY_LOCK_KEY,))
        try:
            # The bookkeeping table lives in the schema 001 creates, so make both first.
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            ledger = table(schema, "schema_migration")
            conn.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} (version text PRIMARY KEY, "
                    "checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
                ).format(ledger)
            )
            done: dict[str, str] = dict(
                conn.execute(sql.SQL("SELECT version, checksum FROM {}").format(ledger)).fetchall()
            )
            for path in files:
                text = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if path.stem in done:
                    if done[path.stem] != checksum:
                        raise ConfigurationError(
                            f"migration {path.name} was modified after being applied; "
                            "add a new migration instead of editing an old one"
                        )
                    continue
                with conn.transaction():
                    conn.execute(text.encode("utf-8"))
                    conn.execute(
                        sql.SQL("INSERT INTO {} (version, checksum) VALUES (%s, %s)").format(
                            ledger
                        ),
                        (path.stem, checksum),
                    )
                applied.append(path.stem)
        finally:
            conn.execute(sql.SQL("SELECT pg_advisory_unlock(%s)"), (_ADVISORY_LOCK_KEY,))
    return applied


def main() -> int:
    settings = get_settings()
    versions = apply_migrations(settings.require_dsn(), schema=settings.db_schema)
    print("applied: " + (", ".join(versions) if versions else "nothing (up to date)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
