from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

ACTIVE_STATES = ("assigned", "running", "verifying")
RUN_ID = "BUR-RUN-BENCHMARK"
WORKER_ID = "worker-a"
LEGACY_RUN_READ = "SELECT * FROM runs WHERE run_id=?"
CAS_RUN_UPDATE = """
UPDATE runs SET heartbeat_at=?,updated_at=?
WHERE run_id=? AND state IN (?,?,?) AND worker_id=?
"""


@dataclass
class FullRunReadCounter:
    count: int = 0

    def trace(self, statement: str) -> None:
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("SELECT * FROM RUNS WHERE RUN_ID="):
            self.count += 1


def database() -> tuple[sqlite3.Connection, FullRunReadCounter]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=MEMORY")
    connection.execute("PRAGMA synchronous=OFF")
    connection.executescript(
        """
        CREATE TABLE workers(
            worker_id TEXT PRIMARY KEY,
            heartbeat_at TEXT NOT NULL
        );
        CREATE TABLE runs(
            run_id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            state TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL
        );
        """
    )
    connection.execute("INSERT INTO workers VALUES(?,?)", (WORKER_ID, "initial"))
    connection.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?)",
        (RUN_ID, WORKER_ID, "assigned", "initial", "initial"),
    )
    counter = FullRunReadCounter()
    connection.set_trace_callback(counter.trace)
    return connection, counter


def legacy_heartbeat(connection: sqlite3.Connection, stamp: str) -> None:
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(LEGACY_RUN_READ, (RUN_ID,)).fetchone()
    if row is None or row["state"] not in ACTIVE_STATES or row["worker_id"] != WORKER_ID:
        raise RuntimeError("legacy heartbeat precondition failed")
    connection.execute(
        "UPDATE runs SET heartbeat_at=?,updated_at=? WHERE run_id=?",
        (stamp, stamp, RUN_ID),
    )
    connection.execute(
        "UPDATE workers SET heartbeat_at=? WHERE worker_id=?",
        (stamp, row["worker_id"]),
    )
    connection.execute(
        "INSERT INTO events(run_id,event_type) VALUES(?,?)",
        (RUN_ID, "run-heartbeat"),
    )
    connection.commit()
    if connection.execute(LEGACY_RUN_READ, (RUN_ID,)).fetchone() is None:
        raise RuntimeError("legacy heartbeat readback failed")


def cas_heartbeat(connection: sqlite3.Connection, stamp: str) -> None:
    connection.execute("BEGIN IMMEDIATE")
    updated = connection.execute(
        CAS_RUN_UPDATE,
        (stamp, stamp, RUN_ID, *ACTIVE_STATES, WORKER_ID),
    )
    if updated.rowcount != 1:
        raise RuntimeError("CAS heartbeat precondition failed")
    connection.execute(
        """
        UPDATE workers SET heartbeat_at=?
        WHERE worker_id=(SELECT worker_id FROM runs WHERE run_id=?)
        """,
        (stamp, RUN_ID),
    )
    connection.execute(
        "INSERT INTO events(run_id,event_type) VALUES(?,?)",
        (RUN_ID, "run-heartbeat"),
    )
    connection.commit()
    if connection.execute(LEGACY_RUN_READ, (RUN_ID,)).fetchone() is None:
        raise RuntimeError("CAS heartbeat readback failed")


def duration_ns(
    operation: Callable[[sqlite3.Connection, str], None],
    connection: sqlite3.Connection,
    stamp: str,
) -> int:
    started = time.perf_counter_ns()
    operation(connection, stamp)
    return time.perf_counter_ns() - started


def percentile_95(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def benchmark(iterations: int) -> dict[str, object]:
    legacy_connection, legacy_reads = database()
    cas_connection, cas_reads = database()
    legacy_durations: list[int] = []
    cas_durations: list[int] = []
    try:
        for iteration in range(iterations):
            stamp = f"heartbeat-{iteration:05d}"
            operations = (
                (
                    (legacy_heartbeat, legacy_connection, legacy_durations),
                    (cas_heartbeat, cas_connection, cas_durations),
                )
                if iteration % 2 == 0
                else (
                    (cas_heartbeat, cas_connection, cas_durations),
                    (legacy_heartbeat, legacy_connection, legacy_durations),
                )
            )
            for operation, connection, durations in operations:
                durations.append(duration_ns(operation, connection, stamp))
    finally:
        legacy_connection.close()
        cas_connection.close()

    legacy_p95 = percentile_95(legacy_durations)
    cas_p95 = percentile_95(cas_durations)
    read_reduction = 1 - (cas_reads.count / legacy_reads.count)
    p95_regression = (cas_p95 / legacy_p95) - 1
    passed = (
        legacy_reads.count == iterations * 2
        and cas_reads.count == iterations
        and read_reduction == 0.5
        and p95_regression <= 0.10
    )
    return {
        "schema_version": 1,
        "kind": "bureau_heartbeat_cas_benchmark",
        "iterations_per_variant": iterations,
        "legacy": {
            "full_runs_row_reads": legacy_reads.count,
            "full_runs_row_reads_per_success": legacy_reads.count / iterations,
            "p95_nanoseconds": legacy_p95,
        },
        "cas": {
            "full_runs_row_reads": cas_reads.count,
            "full_runs_row_reads_per_success": cas_reads.count / iterations,
            "p95_nanoseconds": cas_p95,
        },
        "read_reduction_percent": read_reduction * 100,
        "p95_regression_percent": p95_regression * 100,
        "limits": {
            "required_read_reduction_percent": 50.0,
            "maximum_p95_regression_percent": 10.0,
        },
        "decision": "pass" if passed else "fail",
        "scope": "exact heartbeat SQL statements in balanced in-memory SQLite transactions",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10_000)
    arguments = parser.parse_args()
    if arguments.iterations < 1:
        parser.error("--iterations must be positive")
    result = benchmark(arguments.iterations)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["decision"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
