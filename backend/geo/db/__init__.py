"""Postgres access. One pool per process; callers borrow connections with `connection()`."""

from __future__ import annotations

import atexit
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from geo.config import settings

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings().database_url,
            min_size=1,
            max_size=settings().db_pool_max,
            # Supabase's pooler (pgbouncer, transaction mode) does not support prepared statements.
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )
        atexit.register(_pool.close)
    return _pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection inside a transaction: commits on success, rolls back on error."""
    with pool().connection() as conn:
        yield conn
