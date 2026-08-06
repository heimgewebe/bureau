from __future__ import annotations

import argparse
import ast
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from . import legacy

_SCHEMA_VERSION = 1
_WRITE_CALLS = {
    "open",
    "replace",
    "rename",
    "unlink",
    "write_bytes",
    "write_text",
    "writelines",
}
_REGISTRY_KINDS = {"initiatives", "queue", "resources", "sources", "tasks"}
_STATE_TABLES = (
    "task_status",
    "runs",
    "events",
    "task_claims",
    "receipt_records",
)


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _registry_literal(strings: set[str]) -> bool:
    lowered = {value.lower() for value in strings}
    if any("registry/" in value or "/registry/" in value for value in lowered):
        return True
    return "registry" in lowered and bool(_REGISTRY_KINDS.intersection(lowered))


def _classify_consumer(record: dict[str, Any]) -> None:
    writes = set(record["writes"])
    kind = record["kind"]
    if "state_store" in writes and "git_registry" in writes:
        target = "state-store-api"
        disposition = "split-dual-writer-and-remove-operational-git-write"
    elif "state_store" in writes:
        target = "state-store-api"
        disposition = "retain-or-converge-as-authoritative-state-writer"
    elif "git_registry" in writes:
        target = "state-store-api"
        disposition = "remove-operational-git-write-after-cutover"
    elif "github_transport" in writes:
        target = "github-code-ci-or-redacted-snapshot-transport"
        disposition = "retain-only-as-bounded-transport"
    elif kind == "systemd-unit":
        target = "typed-bureau-cli-or-read-only-projection"
        disposition = "retain-as-runtime-consumer"
    elif kind == "github-workflow":
        target = "github-code-ci-or-redacted-snapshot-validation"
        disposition = "retain-as-independent-validator"
    elif record["path"].startswith("external:"):
        target = record.get("declared_target", "typed-read-only-projection")
        disposition = "external-contract"
    else:
        target = "typed-read-only-projection"
        disposition = "retain-as-observer"
    record["target_authority"] = target
    record["migration_disposition"] = disposition


def _scan_python(
    path: Path,
    relative: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
    except (OSError, UnicodeError, SyntaxError) as exc:
        return None, {
            "severity": "error",
            "code": "python-scan-failed",
            "path": relative,
            "detail": f"{type(exc).__name__}: {exc}",
        }

    calls = {
        _call_name(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    call_tails = {name.rsplit(".", 1)[-1] for name in calls if name}
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    lowered_source = source.lower()
    registry_reference = (
        _registry_literal(strings)
        or "registry.load" in lowered_source
        or "bureau_registry_root" in lowered_source
    )
    state_reference = bool(
        {"StateStore", "ReadOnlyStateStore"}.intersection(call_tails)
        or "bureau_state_dir" in lowered_source
        or "bureau.sqlite3" in lowered_source
        or "state_db" in lowered_source
        or "state_root" in lowered_source
    )
    github_reference = any(
        token in lowered_source
        for token in (
            "github",
            "gh pr",
            "git push",
            "pull_request",
            "source-pr-bridge",
        )
    )
    grabowski_reference = "grabowski" in lowered_source

    reads: set[str] = set()
    writes: set[str] = set()
    if registry_reference:
        reads.add("git_registry")
    if state_reference:
        reads.add("state_store")
    if github_reference:
        reads.add("github")
    if grabowski_reference:
        reads.add("grabowski")
    if "StateStore" in call_tails:
        writes.add("state_store")
    if registry_reference and _WRITE_CALLS.intersection(call_tails):
        writes.add("git_registry")
    if github_reference and any(
        token in lowered_source
        for token in ("git push", "gh pr create", "create_pull_request", "update_ref")
    ):
        writes.add("github_transport")

    if not reads and not writes:
        return None, None
    record = {
        "path": relative,
        "kind": "python-module",
        "reads": sorted(reads),
        "writes": sorted(writes),
        "evidence": {
            "call_markers": sorted(
                marker
                for marker in call_tails
                if marker
                in {
                    "ReadOnlyStateStore",
                    "Registry",
                    "StateStore",
                    "open",
                    "replace",
                    "rename",
                    "unlink",
                    "write_bytes",
                    "write_text",
                }
            ),
            "registry_reference": registry_reference,
            "state_reference": state_reference,
            "github_reference": github_reference,
            "grabowski_reference": grabowski_reference,
        },
    }
    _classify_consumer(record)
    return record, None


def _scan_workflow(path: Path, relative: str) -> dict[str, Any] | None:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    lowered = source.lower()
    if "bureau" not in lowered and "registry" not in lowered:
        return None
    reads: set[str] = {"github"}
    writes: set[str] = set()
    if "registry" in lowered:
        reads.add("git_registry")
    if "contents: write" in lowered or "git push" in lowered or "gh pr" in lowered:
        writes.add("github_transport")
    if "registry" in lowered and writes:
        writes.add("git_registry")
    record = {
        "path": relative,
        "kind": "github-workflow",
        "reads": sorted(reads),
        "writes": sorted(writes),
        "evidence": {
            "pull_request": "pull_request" in lowered,
            "pull_request_target": "pull_request_target" in lowered,
            "push": "push:" in lowered,
            "contents_write": "contents: write" in lowered,
            "git_push": "git push" in lowered,
            "gh_pr": "gh pr" in lowered,
        },
    }
    _classify_consumer(record)
    return record


def _scan_unit(path: Path, relative: str) -> dict[str, Any] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    exec_start = [
        line.split("=", 1)[1].strip()
        for line in lines
        if line.startswith("ExecStart=")
    ]
    if not exec_start and path.suffix not in {".service", ".timer"}:
        return None
    joined = "\n".join(lines).lower()
    reads = ["runtime"]
    if "bureau" in joined:
        reads.extend(["git_registry", "state_store"])
    record = {
        "path": relative,
        "kind": "systemd-unit",
        "reads": sorted(set(reads)),
        "writes": ["delegated_cli"] if exec_start else [],
        "evidence": {
            "unit_name": path.name,
            "exec_start": exec_start,
        },
    }
    _classify_consumer(record)
    return record


def _git_head(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if len(value) == 40 else None


def _state_probe(state_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(state_path),
        "available": False,
        "read_only": True,
    }
    if not state_path.is_file():
        result["reason"] = "missing"
        return result
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        result["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
        result["schema_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
        result["foreign_key_error_count"] = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        result["tables"] = sorted(tables)
        result["table_counts"] = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in _STATE_TABLES
            if table in tables
        }
        result["available"] = True
    except sqlite3.Error as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        if connection is not None:
            connection.close()
    return result


def _parse_systemd_show(stdout: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in [*stdout.splitlines(), ""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            current[key] = value
    return records


def _systemd_probe(root: Path, *, enabled: bool) -> dict[str, Any]:
    unit_root = root / "ops/systemd"
    unit_names = sorted(
        path.name
        for path in unit_root.glob("bureau-*")
        if path.suffix in {".service", ".timer"}
    )
    result: dict[str, Any] = {
        "enabled": enabled,
        "read_only": True,
        "declared_units": unit_names,
        "live_available": False,
        "units": [],
    }
    if not enabled or not unit_names:
        result["reason"] = "disabled" if not enabled else "no-declared-units"
        return result
    try:
        completed = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                "--no-pager",
                "--property=Id,LoadState,ActiveState,SubState,UnitFileState,FragmentPath",
                *unit_names,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        result["reason"] = (
            detail[0][:300] if detail else f"systemctl-exit-{completed.returncode}"
        )
        return result
    result["units"] = _parse_systemd_show(completed.stdout)
    result["live_available"] = True
    return result


def _external_consumers() -> list[dict[str, Any]]:
    records = [
        {
            "path": "external:grabowski",
            "kind": "external-authority",
            "reads": ["execution_envelopes", "task_claims"],
            "writes": ["runtime_effects", "resource_leases", "workspaces"],
            "declared_target": "grabowski-runtime-authority",
            "evidence": {"contract": "Bureau README external-authority boundary"},
        },
        {
            "path": "external:github-actions",
            "kind": "external-authority",
            "reads": ["github", "git_registry"],
            "writes": ["ci_results"],
            "declared_target": "github-code-ci-or-redacted-snapshot-validation",
            "evidence": {"contract": "public workflow checks"},
        },
        {
            "path": "external:heim-pc-dashboard",
            "kind": "external-consumer",
            "reads": ["read_only_projection"],
            "writes": [],
            "declared_target": "typed-read-only-projection",
            "evidence": {"contract": "dashboard read-only boundary"},
        },
    ]
    for record in records:
        _classify_consumer(record)
        record.pop("declared_target", None)
    return records


def authority_inventory(
    root: Path,
    *,
    state_path: Path,
    probe_systemd: bool = True,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    consumers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    source_root = root / "src/bureau"
    if source_root.is_dir():
        for path in sorted(source_root.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            record, error = _scan_python(path, relative)
            if record is not None:
                consumers.append(record)
            if error is not None:
                findings.append(error)

    workflow_root = root / ".github/workflows"
    if workflow_root.is_dir():
        for path in sorted(
            [*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")]
        ):
            record = _scan_workflow(path, path.relative_to(root).as_posix())
            if record is not None:
                consumers.append(record)

    unit_root = root / "ops/systemd"
    if unit_root.is_dir():
        for path in sorted(unit_root.iterdir()):
            if path.is_file():
                record = _scan_unit(path, path.relative_to(root).as_posix())
                if record is not None:
                    consumers.append(record)

    consumers.extend(_external_consumers())
    consumers.sort(key=lambda item: item["path"])

    for record in consumers:
        writes = set(record["writes"])
        if {"git_registry", "state_store"}.issubset(writes):
            findings.append(
                {
                    "severity": "warning",
                    "code": "dual-operational-writer",
                    "path": record["path"],
                    "detail": (
                        "consumer can write both Git Registry and StateStore "
                        "during migration"
                    ),
                }
            )
        elif "git_registry" in writes:
            findings.append(
                {
                    "severity": "info",
                    "code": "operational-git-writer-to-migrate",
                    "path": record["path"],
                    "detail": (
                        "operational Git write must be removed or reduced "
                        "to snapshot transport"
                    ),
                }
            )

    state = _state_probe(state_path.expanduser())
    systemd = _systemd_probe(root, enabled=probe_systemd)
    if not systemd["live_available"]:
        findings.append(
            {
                "severity": "info",
                "code": "systemd-live-state-unavailable",
                "path": "systemd:user",
                "detail": systemd.get("reason", "unknown"),
            }
        )

    error_count = sum(1 for item in findings if item["severity"] == "error")
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "bureau_authority_inventory",
        "status": "complete" if error_count == 0 else "incomplete",
        "complete": error_count == 0,
        "read_only": True,
        "repository": {
            "root": str(root),
            "head": _git_head(root),
        },
        "authorities": [
            {
                "subject": "code-schemas-releases",
                "current": "github-main",
                "target": "github-main",
            },
            {
                "subject": "task-status-frontier-claims-runs-acceptance-closeout",
                "current": "split-git-registry-and-state-store",
                "target": "bureau-state-store",
            },
            {
                "subject": "processes-workspaces-concrete-leases-host-effects",
                "current": "grabowski",
                "target": "grabowski",
            },
            {
                "subject": "public-operational-view",
                "current": "git-registry-and-dashboard-projections",
                "target": "redacted-hash-bound-snapshot",
            },
        ],
        "consumers": consumers,
        "state_store": state,
        "systemd": systemd,
        "findings": sorted(
            findings,
            key=lambda item: (item["severity"], item["code"], item["path"]),
        ),
        "summary": {
            "consumer_count": len(consumers),
            "state_writer_count": sum(
                "state_store" in item["writes"] for item in consumers
            ),
            "git_registry_writer_count": sum(
                "git_registry" in item["writes"] for item in consumers
            ),
            "github_transport_count": sum(
                "github_transport" in item["writes"] for item in consumers
            ),
            "systemd_unit_count": sum(
                item["kind"] == "systemd-unit" for item in consumers
            ),
            "error_count": error_count,
            "warning_count": sum(
                1 for item in findings if item["severity"] == "warning"
            ),
            "migration_required_count": sum(
                item["migration_disposition"]
                in {
                    "remove-operational-git-write-after-cutover",
                    "split-dual-writer-and-remove-operational-git-write",
                }
                for item in consumers
            ),
        },
        "does_not_establish": [
            "mutation_authority",
            "consumer_runtime_health_when_live_probe_is_unavailable",
            "cutover_readiness",
            "safe_removal_of_any_writer",
        ],
    }
    payload["inventory_sha256"] = legacy.sha256_json(payload)
    return payload


def _state_path(args: argparse.Namespace) -> Path:
    if args.state_db:
        return Path(args.state_db).expanduser()
    if args.state_root:
        return Path(args.state_root).expanduser() / "bureau.sqlite3"
    return Path("~/.local/state/bureau/bureau.sqlite3").expanduser()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="python -m bureau.authority_inventory")
    result.add_argument("--root", default=".")
    result.add_argument("--state-db")
    result.add_argument("--state-root")
    result.add_argument("--skip-systemd", action="store_true")
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    value = authority_inventory(
        Path(args.root),
        state_path=_state_path(args),
        probe_systemd=not args.skip_systemd,
    )
    if args.json:
        print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        summary = value["summary"]
        print(f"status: {value['status']}")
        print(f"consumers: {summary['consumer_count']}")
        print(f"state_writers: {summary['state_writer_count']}")
        print(f"git_registry_writers: {summary['git_registry_writer_count']}")
        print(f"migration_required: {summary['migration_required_count']}")
        print(f"inventory_sha256: {value['inventory_sha256']}")
    return 0 if value["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
