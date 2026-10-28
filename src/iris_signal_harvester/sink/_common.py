"""Small shared helpers for the sink package (connection + identifier quoting)."""

from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

__all__ = ["DEFAULT_SCHEMA", "Conn", "connect", "table", "validate_schema"]

DEFAULT_SCHEMA = "iris_staging"

Conn = psycopg.Connection[TupleRow]

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def validate_schema(schema: str) -> str:
    """Reject anything that is not a plain lower-case identifier."""
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"invalid schema name: {schema!r}")
    return schema


def connect(dsn: str, **kwargs: Any) -> Conn:
    """Open a connection. The caller owns it and must close it (use ``with``)."""
    return psycopg.connect(dsn, **kwargs)


def table(schema: str, name: str) -> sql.Composed:
    """A safely quoted ``schema.name`` reference."""
    return sql.SQL("{}.{}").format(sql.Identifier(validate_schema(schema)), sql.Identifier(name))
