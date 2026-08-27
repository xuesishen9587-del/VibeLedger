"""Report credential-safe network and PostgreSQL latency diagnostics."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import statistics
import sys
import time
from typing import Any
from urllib.parse import urlsplit

import psycopg2


def duration_stats(values: list[float]) -> dict[str, float | int]:
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


def classify_host(host: str) -> str:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect-samples", type=int, default=5)
    parser.add_argument("--select-samples", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output", help="Optional path for the JSON diagnostics report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL must be explicitly configured.", file=sys.stderr)
        return 2

    parsed = urlsplit(database_url)
    host = parsed.hostname
    if not host:
        print("DATABASE_URL does not contain a valid hostname.", file=sys.stderr)
        return 2
    port = parsed.port or 5432

    dns_started = time.perf_counter()
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    dns_seconds = time.perf_counter() - dns_started

    tcp_seconds = []
    for _ in range(args.connect_samples):
        started = time.perf_counter()
        with socket.create_connection((host, port), timeout=args.timeout):
            pass
        tcp_seconds.append(time.perf_counter() - started)

    postgres_seconds = []
    ssl_in_use = None
    server_version = None
    for _ in range(args.connect_samples):
        started = time.perf_counter()
        conn = psycopg2.connect(database_url, connect_timeout=max(1, math.ceil(args.timeout)))
        postgres_seconds.append(time.perf_counter() - started)
        try:
            ssl_in_use = bool(getattr(conn.info, "ssl_in_use", False))
            server_version = conn.server_version
        finally:
            conn.close()

    conn = psycopg2.connect(database_url, connect_timeout=max(1, math.ceil(args.timeout)))
    select_seconds = []
    try:
        with conn.cursor() as cursor:
            for _ in range(args.select_samples):
                started = time.perf_counter()
                cursor.execute("SELECT 1;")
                cursor.fetchone()
                select_seconds.append(time.perf_counter() - started)
    finally:
        conn.close()

    report: dict[str, Any] = {
        "environment": os.environ.get("ENVIRONMENT", "missing"),
        "db_schema": os.environ.get("DB_SCHEMA", "missing"),
        "host_type": classify_host(host),
        "port": port,
        "dns": {
            "duration_seconds": round(dns_seconds, 6),
            "resolved_address_count": len(addresses),
        },
        "tcp_connect": duration_stats(tcp_seconds),
        "postgres_connect": duration_stats(postgres_seconds),
        "select_one": duration_stats(select_seconds),
        "ssl_in_use": ssl_in_use,
        "server_version_num": server_version,
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }

    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        destination = Path(args.output).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")
        print(f"DIAGNOSTICS_OUTPUT={destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
