"""Run integration tests with opt-in, credential-safe database profiling.

This runner preserves the normal unittest lifecycle and real PostgreSQL
connections. It only observes connection, SQL, migration, fixture, cleanup,
HTTP, and per-test timing. No repository or transaction boundary is mocked.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import statistics
import sys
import threading
import time
from typing import Any, Iterable
import unittest
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _duration_stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {
            "count": 0,
            "total_seconds": 0.0,
            "mean_seconds": 0.0,
            "median_seconds": 0.0,
            "p95_seconds": 0.0,
            "max_seconds": 0.0,
        }
    ordered = sorted(values)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "count": len(ordered),
        "total_seconds": round(sum(ordered), 6),
        "mean_seconds": round(statistics.fmean(ordered), 6),
        "median_seconds": round(statistics.median(ordered), 6),
        "p95_seconds": round(ordered[p95_index], 6),
        "max_seconds": round(ordered[-1], 6),
    }


def _classify_host(host: str | None) -> str:
    if not host:
        return "missing"
    normalized = host.strip().lower()
    if normalized == "localhost":
        return "local"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return "remote_dns"
    if address.is_loopback:
        return "local"
    if address.is_private:
        return "private_network"
    return "remote_ip"


def _sanitized_database_environment() -> dict[str, Any]:
    raw_url = os.environ.get("DATABASE_URL", "")
    parsed = urlsplit(raw_url)
    query = parsed.query.lower()
    ssl = "required" if "sslmode=require" in query else "unspecified"
    return {
        "environment": os.environ.get("ENVIRONMENT", "missing"),
        "db_schema": os.environ.get("DB_SCHEMA", "missing"),
        "host_type": _classify_host(parsed.hostname),
        "port": parsed.port or 5432,
        "ssl": ssl,
    }


class HarnessProfiler:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.connection_seconds: list[float] = []
        self.connection_failures = 0
        self.http_connection_seconds: list[float] = []
        self.logical_connection_seconds: list[float] = []
        self.logical_connection_failures = 0
        self.logical_http_connection_seconds: list[float] = []
        self.sql_seconds: list[float] = []
        self.schema_create_seconds: list[float] = []
        self.schema_drop_seconds: list[float] = []
        self.migration_seconds: list[float] = []
        self.truncate_seconds: list[float] = []
        self.seed_seconds: list[float] = []
        self.setup_seconds: list[float] = []
        self.http_seconds: list[float] = []
        self.test_seconds: dict[str, float] = {}
        self._active_http_requests = 0

    def append(self, target: str, duration: float) -> None:
        with self.lock:
            getattr(self, target).append(duration)

    def record_connection(self, duration: float, success: bool) -> None:
        with self.lock:
            self.connection_seconds.append(duration)
            if not success:
                self.connection_failures += 1
            if self._active_http_requests:
                self.http_connection_seconds.append(duration)

    def record_logical_connection(self, duration: float, success: bool) -> None:
        with self.lock:
            self.logical_connection_seconds.append(duration)
            if not success:
                self.logical_connection_failures += 1
            if self._active_http_requests:
                self.logical_http_connection_seconds.append(duration)

    def begin_http(self) -> None:
        with self.lock:
            self._active_http_requests += 1

    def end_http(self, duration: float) -> None:
        with self.lock:
            self.http_seconds.append(duration)
            self._active_http_requests -= 1

    def record_test(self, test_id: str, duration: float) -> None:
        with self.lock:
            self.test_seconds[test_id] = duration


def _flatten_suite(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    tests: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            tests.extend(_flatten_suite(item))
        else:
            tests.append(item)
    return tests


def _load_suite(loader: unittest.TestLoader, names: list[str], pattern: str) -> unittest.TestSuite:
    if not names:
        return loader.discover("tests/integration", pattern=pattern)

    normalized = []
    for name in names:
        candidate = name.replace("\\", "/")
        if candidate.endswith(".py"):
            candidate = candidate[:-3]
        if candidate.startswith("./"):
            candidate = candidate[2:]
        candidate = candidate.replace("/", ".")
        normalized.append(candidate)
    return loader.loadTestsFromNames(normalized)


def _wrap_instance_method(cls: type, method_name: str, profiler: HarnessProfiler, metric: str) -> None:
    original = getattr(cls, method_name, None)
    if original is None:
        return

    def measured(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return original(self, *args, **kwargs)
        finally:
            profiler.append(metric, time.perf_counter() - started)

    measured.__name__ = getattr(original, "__name__", method_name)
    measured.__doc__ = getattr(original, "__doc__", None)
    setattr(cls, method_name, measured)


class ProfilingTextTestResult(unittest.TextTestResult):
    def __init__(self, *args, profiler: HarnessProfiler, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.profiler = profiler
        self._test_started: dict[str, float] = {}

    def startTest(self, test) -> None:
        self._test_started[test.id()] = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test) -> None:
        started = self._test_started.pop(test.id(), None)
        if started is not None:
            self.profiler.record_test(test.id(), time.perf_counter() - started)
        super().stopTest(test)


class ProfilingTextTestRunner(unittest.TextTestRunner):
    def __init__(self, *args, profiler: HarnessProfiler, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.profiler = profiler

    def _makeResult(self):
        return ProfilingTextTestResult(
            self.stream,
            self.descriptions,
            self.verbosity,
            profiler=self.profiler,
        )


def _install_instrumentation(profiler: HarnessProfiler, pool_size: int):
    import psycopg2
    from psycopg2.extensions import connection as PsycopgConnection
    from psycopg2.extensions import cursor as PsycopgCursor

    original_connect = psycopg2.connect

    class ProfilingCursor(PsycopgCursor):
        def execute(self, query, vars=None):
            started = time.perf_counter()
            try:
                return super().execute(query, vars)
            finally:
                profiler.append("sql_seconds", time.perf_counter() - started)

        def executemany(self, query, vars_list):
            started = time.perf_counter()
            try:
                return super().executemany(query, vars_list)
            finally:
                profiler.append("sql_seconds", time.perf_counter() - started)

        def callproc(self, procname, parameters=None):
            started = time.perf_counter()
            try:
                return super().callproc(procname, parameters)
            finally:
                profiler.append("sql_seconds", time.perf_counter() - started)

    class ProfilingConnection(PsycopgConnection):
        def cursor(self, *args, **kwargs):
            kwargs.setdefault("cursor_factory", ProfilingCursor)
            return super().cursor(*args, **kwargs)

    def profiled_connect(*args, **kwargs):
        started = time.perf_counter()
        success = False
        call_kwargs = dict(kwargs)
        call_kwargs.setdefault("connection_factory", ProfilingConnection)
        try:
            connection = original_connect(*args, **call_kwargs)
            success = True
            return connection
        finally:
            duration = time.perf_counter() - started
            profiler.record_connection(duration, success)
            if pool_size == 0:
                profiler.record_logical_connection(duration, success)

    psycopg2.connect = profiled_connect

    pool_controller = None
    if pool_size:
        from tests.support.database_pool import install_test_connection_pool

        pool_controller = install_test_connection_pool(
            pool_size,
            connector=profiled_connect,
            logical_timing_observer=profiler.record_logical_connection,
        )

    from migrations import runner as migration_runner

    original_migrate = migration_runner.run_migrations

    def profiled_migrate(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_migrate(*args, **kwargs)
        finally:
            profiler.append("migration_seconds", time.perf_counter() - started)

    migration_runner.run_migrations = profiled_migrate

    from tests.support import db_helper

    def wrap_function(name: str, metric: str) -> None:
        original = getattr(db_helper, name)

        def measured(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                profiler.append(metric, time.perf_counter() - started)

        setattr(db_helper, name, measured)

    wrap_function("create_test_schema", "schema_create_seconds")
    wrap_function("drop_test_schema", "schema_drop_seconds")
    wrap_function("truncate_business_tables", "truncate_seconds")

    try:
        from starlette.testclient import TestClient
    except ImportError:
        TestClient = None

    if TestClient is not None:
        original_request = TestClient.request

        def profiled_request(self, *args, **kwargs):
            profiler.begin_http()
            started = time.perf_counter()
            try:
                return original_request(self, *args, **kwargs)
            finally:
                profiler.end_http(time.perf_counter() - started)

        TestClient.request = profiled_request

    return db_helper, pool_controller


def _build_report(
    profiler: HarnessProfiler,
    result: unittest.TestResult,
    test_names: list[str],
    wall_seconds: float,
    top_count: int,
    pool_size: int,
) -> dict[str, Any]:
    inventory_bytes = "\n".join(sorted(test_names)).encode("utf-8")
    slowest = sorted(profiler.test_seconds.items(), key=lambda item: item[1], reverse=True)[:top_count]
    return {
        "environment": {
            **_sanitized_database_environment(),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "shared_schema": os.environ.get("VIBELEDGER_TEST_SHARED_SCHEMA", "").lower()
            in {"1", "true", "yes", "on"},
            "connection_pool_size": pool_size,
        },
        "tests": {
            "discovered": len(test_names),
            "run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "successful": result.wasSuccessful(),
            "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
            "wall_seconds": round(wall_seconds, 6),
        },
        "physical_connections": {
            **_duration_stats(profiler.connection_seconds),
            "failures": profiler.connection_failures,
            "during_http_requests": _duration_stats(profiler.http_connection_seconds),
        },
        "logical_connection_checkouts": {
            **_duration_stats(profiler.logical_connection_seconds),
            "failures": profiler.logical_connection_failures,
            "during_http_requests": _duration_stats(profiler.logical_http_connection_seconds),
        },
        "sql_execute_calls": _duration_stats(profiler.sql_seconds),
        "http_requests": _duration_stats(profiler.http_seconds),
        "schema_creation": _duration_stats(profiler.schema_create_seconds),
        "schema_drop": _duration_stats(profiler.schema_drop_seconds),
        "migrations": _duration_stats(profiler.migration_seconds),
        "truncate_reset": _duration_stats(profiler.truncate_seconds),
        "fixture_seed": _duration_stats(profiler.seed_seconds),
        "test_setup": _duration_stats(profiler.setup_seconds),
        "slowest_tests": [
            {"test": test_id, "seconds": round(duration, 6)}
            for test_id, duration in slowest
        ],
        "skipped_tests": [
            {"test": test.id(), "reason": reason}
            for test, reason in result.skipped
        ],
    }


def _write_json(path: str, report: dict[str, Any]) -> None:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Optional unittest module names or test file paths.")
    parser.add_argument("--pattern", default="test_*.py", help="Discovery pattern for the full suite.")
    parser.add_argument("--output", help="Optional path for the JSON profile report.")
    parser.add_argument("--top", type=int, default=30, help="Number of slowest tests to report.")
    parser.add_argument("--verbosity", type=int, choices=(0, 1, 2), default=1)
    parser.add_argument(
        "--pool-size",
        type=int,
        choices=range(0, 65),
        default=0,
        metavar="0..64",
        help="Opt-in maximum real PostgreSQL connections per unique connection spec.",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, str(PROJECT_ROOT))

    if os.environ.get("ENVIRONMENT") != "test":
        print("Refusing to profile integration tests unless ENVIRONMENT=test.", file=sys.stderr)
        return 2
    if not os.environ.get("DATABASE_URL") or not os.environ.get("DB_SCHEMA"):
        print("DATABASE_URL and DB_SCHEMA must be explicitly configured.", file=sys.stderr)
        return 2

    profiler = HarnessProfiler()
    db_helper, pool_controller = _install_instrumentation(profiler, args.pool_size)
    loader = unittest.defaultTestLoader
    suite = _load_suite(loader, args.tests, args.pattern)
    tests = _flatten_suite(suite)
    test_names = [test.id() for test in tests]

    concrete_classes = {test.__class__ for test in tests if isinstance(test, db_helper.BaseDbTestCase)}
    for cls in concrete_classes:
        _wrap_instance_method(cls, "seed_test_data", profiler, "seed_seconds")
        _wrap_instance_method(cls, "setUp", profiler, "setup_seconds")

    runner = ProfilingTextTestRunner(verbosity=args.verbosity, profiler=profiler)
    started = time.perf_counter()
    try:
        result = runner.run(suite)
    finally:
        try:
            cleanup_shared = getattr(db_helper, "cleanup_shared_test_schema", None)
            if cleanup_shared is not None:
                cleanup_shared()
        finally:
            if pool_controller is not None:
                pool_controller.close_and_restore()
    wall_seconds = time.perf_counter() - started

    report = _build_report(profiler, result, test_names, wall_seconds, args.top, args.pool_size)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print("\nPROFILE_SUMMARY")
    print(rendered)
    if args.output:
        _write_json(args.output, report)
        print(f"PROFILE_OUTPUT={Path(args.output).resolve()}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
