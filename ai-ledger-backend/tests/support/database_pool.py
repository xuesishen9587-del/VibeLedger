"""Opt-in real-PostgreSQL connection reuse for integration-test runners.

The pool is deliberately installed by a test runner instead of production code.
Every checkout owns one physical psycopg2 connection until ``close()`` returns
it in an idle state; concurrent callers never share a checked-out connection.
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from typing import Any, Callable

import psycopg2
from psycopg2 import extensions
from psycopg2.pool import ThreadedConnectionPool


ConnectCallable = Callable[..., extensions.connection]
TimingObserver = Callable[[float, bool], None]


class _RetainingThreadedConnectionPool(ThreadedConnectionPool):
    """Thread-safe psycopg2 pool that lazily retains up to ``maxconn``."""

    def __init__(self, maxconn: int, connector: ConnectCallable, *args, **kwargs) -> None:
        self._connector = connector
        super().__init__(0, maxconn, *args, **kwargs)

    def _connect(self, key=None):
        conn = self._connector(*self._args, **self._kwargs)
        if key is not None:
            self._used[key] = conn
            self._rused[id(conn)] = key
        else:
            self._pool.append(conn)
        return conn

    def _putconn(self, conn, key=None, close=False):
        if self.closed:
            raise psycopg2.pool.PoolError("connection pool is closed")

        if key is None:
            key = self._rused.get(id(conn))
            if key is None:
                raise psycopg2.pool.PoolError("trying to put unkeyed connection")

        keep = not close and not conn.closed
        if keep:
            status = conn.info.transaction_status
            if status == extensions.TRANSACTION_STATUS_UNKNOWN:
                keep = False
            elif status != extensions.TRANSACTION_STATUS_IDLE:
                try:
                    conn.rollback()
                except Exception:
                    keep = False

        if keep and len(self._pool) < self.maxconn:
            self._pool.append(conn)
        else:
            conn.close()

        self._used.pop(key, None)
        self._rused.pop(id(conn), None)


class _ConnectionLease:
    """Connection-compatible lease whose close returns it to its source pool."""

    __slots__ = ("_connection", "_pool", "_released", "_release_lock")

    def __init__(self, connection, pool: _RetainingThreadedConnectionPool) -> None:
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_released", False)
        object.__setattr__(self, "_release_lock", threading.Lock())

    @property
    def closed(self) -> int:
        if self._released:
            return 1
        return self._connection.closed

    def close(self) -> None:
        with self._release_lock:
            if self._released:
                return
            object.__setattr__(self, "_released", True)
            self._pool.putconn(self._connection)

    def __enter__(self):
        self._require_open()
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._require_open()
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name: str) -> Any:
        self._require_open()
        return getattr(self._connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__:
            object.__setattr__(self, name, value)
            return
        self._require_open()
        setattr(self._connection, name, value)

    def _require_open(self) -> None:
        if self._released:
            raise psycopg2.InterfaceError("connection already closed")


class TestConnectionPoolController:
    """Own the monkeypatch and the pools created for unique connection specs."""

    def __init__(
        self,
        max_size: int,
        connector: ConnectCallable,
        logical_timing_observer: TimingObserver | None = None,
    ) -> None:
        if os.environ.get("ENVIRONMENT") != "test":
            raise RuntimeError("Test connection pooling requires ENVIRONMENT=test.")
        if not 1 <= max_size <= 64:
            raise ValueError("Test connection pool size must be between 1 and 64.")
        self.max_size = max_size
        self._connector = connector
        self._logical_timing_observer = logical_timing_observer
        self._pools: dict[tuple[Any, ...], _RetainingThreadedConnectionPool] = {}
        self._lock = threading.RLock()
        self._closed = False

    def connect(self, *args, **kwargs):
        started = time.perf_counter()
        success = False
        try:
            pool = self._pool_for(args, kwargs)
            lease = _ConnectionLease(pool.getconn(), pool)
            success = True
            return lease
        finally:
            if self._logical_timing_observer is not None:
                self._logical_timing_observer(time.perf_counter() - started, success)

    def close_all(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pools = list(self._pools.values())
            self._pools.clear()
        for pool in pools:
            if not pool.closed:
                pool.closeall()

    def close_and_restore(self) -> None:
        """Restore the connector monkeypatch and close retained connections."""
        installed_on = getattr(psycopg2.connect, "__self__", None)
        if installed_on is self:
            psycopg2.connect = self._connector
        self.close_all()

    def _pool_for(self, args: tuple[Any, ...], kwargs: dict[str, Any]):
        # The key is process-local and is never emitted. Separate pools retain
        # startup options such as the schema-specific search_path.
        key = (
            tuple((type(value).__qualname__, repr(value)) for value in args),
            tuple(
                sorted(
                    (name, type(value).__qualname__, repr(value))
                    for name, value in kwargs.items()
                )
            ),
        )
        with self._lock:
            if self._closed:
                raise psycopg2.InterfaceError("test connection pool is closed")
            pool = self._pools.get(key)
            if pool is None:
                pool = _RetainingThreadedConnectionPool(
                    self.max_size,
                    self._connector,
                    *args,
                    **kwargs,
                )
                self._pools[key] = pool
            return pool


def install_test_connection_pool(
    max_size: int,
    *,
    connector: ConnectCallable | None = None,
    logical_timing_observer: TimingObserver | None = None,
) -> TestConnectionPoolController:
    """Patch ``psycopg2.connect`` until the returned controller is closed."""
    original_connect = connector or psycopg2.connect
    controller = TestConnectionPoolController(
        max_size,
        original_connect,
        logical_timing_observer,
    )
    psycopg2.connect = controller.connect

    atexit.register(controller.close_and_restore)
    return controller
