"""
Read-only PostgreSQL access.

Three independent guards keep the dashboard from ever writing:

1. the session is opened with ``default_transaction_read_only=on`` (server-enforced),
2. the psycopg connection is marked ``read_only`` (BEGIN READ ONLY),
3. ``fetch_all`` refuses any statement that is not a SELECT / WITH ... SELECT.

The DSN is read from ``IRIS_DB_DSN`` (the harvester's variable). It is never logged, never
put into an error message and never written to the page.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ui import config


class DataUnavailable(Exception):
    """The database cannot be used. The message is safe to show to a user."""


_READ_ONLY_STATEMENT = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|truncate|drop|alter|create|grant|revoke|copy|call|do)\b",
    re.IGNORECASE,
)


def _dsn() -> str:
    dsn = os.environ.get(config.DSN_ENV_VAR, "").strip()
    if not dsn:
        raise DataUnavailable(
            f"The environment variable {config.DSN_ENV_VAR} is not set, "
            "so there is no database to read."
        )
    return dsn


def table(name: str) -> sql.Composable:
    """Schema-qualified, safely quoted table identifier."""
    return sql.Identifier(config.DB_SCHEMA, name)


def _assert_read_only(statement: sql.Composable | str, conn: psycopg.Connection[Any]) -> str:
    text = statement.as_string(conn) if isinstance(statement, sql.Composable) else statement
    # Strip string literals before scanning for write keywords (e.g. a 'update' literal).
    scrubbed = re.sub(r"'(?:[^']|'')*'", "''", text)
    if not _READ_ONLY_STATEMENT.match(scrubbed) or _WRITE_KEYWORDS.search(scrubbed):
        raise PermissionError("the dashboard only issues read-only SELECT statements")
    return text


def fetch_all(
    statement: sql.Composable | str, params: Sequence[Any] | None = None
) -> list[dict[str, Any]]:
    """Run one SELECT and return rows as dicts. Raises ``DataUnavailable`` on any DB problem."""
    try:
        with psycopg.connect(
            _dsn(),
            connect_timeout=config.CONNECT_TIMEOUT_S,
            options=(
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={config.STATEMENT_TIMEOUT_MS}"
            ),
            row_factory=dict_row,
        ) as conn:
            conn.read_only = True
            text = _assert_read_only(statement, conn)
            with conn.cursor() as cur:
                cur.execute(text, params)
                return list(cur.fetchall())
    except DataUnavailable:
        raise
    except psycopg.errors.UndefinedTable as exc:
        raise DataUnavailable(
            f"The harvest tables do not exist in schema '{config.DB_SCHEMA}' yet "
            "(apply migrations/001_initial.sql)."
        ) from exc
    except psycopg.Error as exc:
        # str(exc) never contains the password, but keep it to one line anyway.
        first_line = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        raise DataUnavailable(f"The database could not be queried: {first_line}") from exc


def dsn_configured() -> bool:
    """True when ``IRIS_DB_DSN`` is set to something non-blank (the value is never exposed)."""
    return bool(os.environ.get(config.DSN_ENV_VAR, "").strip())


def probe() -> str | None:
    """Try to open the configured database. Returns ``None`` if reachable, else a one-line reason.

    Same read-only session options as ``fetch_all``; issues no statement beyond opening the
    connection. Used by the data-source selection to decide between the database and the
    committed sample files. The DSN never appears in the returned text.
    """
    try:
        with psycopg.connect(
            _dsn(),
            connect_timeout=config.CONNECT_TIMEOUT_S,
            options=(
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={config.STATEMENT_TIMEOUT_MS}"
            ),
        ):
            return None
    except DataUnavailable as exc:
        return str(exc)
    except psycopg.Error as exc:
        first_line = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        return f"the database could not be reached: {first_line}"
