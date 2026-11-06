"""Async wrapper over sqlite3.

sqlite3 connections are thread-bound and SQLite allows one writer at a time,
so each Database owns one connection on one dedicated worker thread. Async
callers submit functions via `run()`; they execute serially on that thread.
This keeps the event loop unblocked without pretending SQLite is concurrent.
"""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar  # noqa: F401  (Callable used in signatures)

T = TypeVar("T")


class Database:
    def __init__(self, path: str | Path, read_only: bool = False) -> None:
        self.path = str(path)
        self.read_only = read_only
        self._executor: ThreadPoolExecutor | None = None  # created per connect(), so a Database can reconnect
        self._conn: sqlite3.Connection | None = None

    async def connect(
        self, schema_sql: str | None = None, init: Callable[[sqlite3.Connection], None] | None = None
    ) -> "Database":
        def _open() -> sqlite3.Connection:
            if self.read_only:
                # Defence in depth: a read-only server cannot write even if a bug tries to.
                uri = Path(self.path).resolve().as_uri() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=True)
            else:
                conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=True)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            if schema_sql:
                conn.executescript(schema_sql)
            if init:
                init(conn)
            return conn

        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"sqlite:{Path(self.path).name}")
        loop = asyncio.get_running_loop()
        self._conn = await loop.run_in_executor(self._executor, _open)
        return self

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        if self._conn is None or self._executor is None:
            raise RuntimeError("database not connected")
        conn = self._conn
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(conn, *args))

    async def close(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, conn.close)
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE takes the write lock up front (no upgrade deadlocks)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
