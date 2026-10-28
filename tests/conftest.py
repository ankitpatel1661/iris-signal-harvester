"""Shared database fixtures.

``test_sink.py`` owns them (a throwaway database per session, dropped afterwards, so a
DSN pointing at something precious is never truncated). They are exposed here so other
test modules can use them by name without re-importing a fixture into their own namespace.
"""

from tests.test_sink import db_dsn, sink

__all__ = ["db_dsn", "sink"]
