"""Run the real-PostgreSQL integration suite with test-only harness reuse."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Iterable
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pattern",
        default="test_*.py",
        help="unittest discovery pattern (default: test_*.py)",
    )
    parser.add_argument(
        "--pool-size",
        type=int,
        choices=range(0, 65),
        default=16,
        metavar="0..64",
        help="Maximum real connections per connection spec; 0 disables reuse.",
    )
    parser.add_argument(
        "--isolated-schema-per-class",
        action="store_true",
        help="Keep the legacy schema-per-class lifecycle instead of one serial-run schema.",
    )
    parser.add_argument("--verbosity", type=int, choices=(0, 1, 2), default=1)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, str(PROJECT_ROOT))

    if os.environ.get("ENVIRONMENT") != "test":
        print("Refusing to run integration tests unless ENVIRONMENT=test.", file=sys.stderr)
        return 2
    if not os.environ.get("DATABASE_URL") or not os.environ.get("DB_SCHEMA"):
        print("DATABASE_URL and DB_SCHEMA must be explicitly configured.", file=sys.stderr)
        return 2

    if args.isolated_schema_per_class:
        os.environ.pop("VIBELEDGER_TEST_SHARED_SCHEMA", None)
    else:
        os.environ["VIBELEDGER_TEST_SHARED_SCHEMA"] = "1"

    pool_controller = None
    if args.pool_size:
        from tests.support.database_pool import install_test_connection_pool

        pool_controller = install_test_connection_pool(args.pool_size)

    suite = unittest.defaultTestLoader.discover("tests/integration", pattern=args.pattern)
    runner = unittest.TextTestRunner(verbosity=args.verbosity)
    try:
        result = runner.run(suite)
    finally:
        try:
            from tests.support.db_helper import cleanup_shared_test_schema

            cleanup_shared_test_schema()
        finally:
            if pool_controller is not None:
                pool_controller.close_and_restore()

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
