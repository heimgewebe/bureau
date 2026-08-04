from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bureau.cycle_deployment import CycleDeploymentError, audit_cycle_deployment

HOME = Path.home()
STATE = HOME / ".local/state/bureau-verifier"
RUNS = STATE / "runs"
OPERATOR_STATE = HOME / ".local/state/bureau-operator"
HEALTH_PATH = HOME / ".local/state/bureau-cycle/health.json"
LEASES = HOME / ".local/state/bureau-cycle/leases"
TASK_DB = HOME / ".local/state/grabowski/tasks.sqlite3"
BUREAU_DB = HOME / ".local/state/bureau/bureau.sqlite3"
HELPER = [sys.executable, "-m", "bureau_cycle.cycle_contract"]
HELPER_ENV = {
    "HOME": str(HOME),
    "PATH": "/usr/bin:/bin",
    "LC_ALL": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
}
CONTRACT_VERSION = 2
SCHEMA_VERSION = 2
TERMINAL_RESULTS = {"completed", "partial", "blocked", "idle", "failed"}
TRANSIENT_UNIT_RE = re.compile(r"^(grabowski-(?:task|job|browser-worker|gui-worker)-).+\.service$")

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def cycle_id() -> str:
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%dT%H")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def helper(args: list[str], *, timeout: int = 45) -> dict[str, Any]:
    proc = subprocess.run(
        HELPER + args,
        cwd=str(HOME),
        env=HELPER_ENV,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    parsed: Any
    try:
        parsed = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr[-2000:],
        "json": parsed,
        "argv": HELPER + args,
    }


def fallback_receipt(reason: str) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"verifier-{stamp}-fallback"
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": cycle_id(),
        "stage": "verifier",
        "run_id": run_id,
        "trigger": os.environ.get("BUREAU_VERIFIER_TRIGGER", "chatgpt-quarter-past"),
        "started_at": utc_now(),
        "finished_at": None,
        "lifecycle_state": "running",
        "result": None,
        "degraded": True,
        "evidence": [{"kind": "begin_helper_failed", "detail": reason[:500]}],
        "next_action": "finish fallback verifier receipt after helper begin failure",
        "receipt_path": str(RUNS / f"{stamp}-{run_id}.json"),
    }


def begin_receipt() -> dict[str, Any]:
    trigger = os.environ.get("BUREAU_VERIFIER_TRIGGER", "chatgpt-quarter-past")
    result = helper(["begin", "--stage", "verifier", "--trigger", trigger])
    if result["returncode"] == 0 and isinstance(result["json"], dict):
        receipt = result["json"]
        receipt.setdefault("evidence", [])
        receipt["evidence"].append(
            {
                "kind": "begin_helper",
                "helper": f"{sys.executable} -m bureau_cycle.cycle_contract begin",
                "returncode": result["returncode"],
            }
        )
        return receipt
    receipt = fallback_receipt((result.get("stderr") or result.get("stdout") or "unknown helper failure"))
    atomic_json(Path(receipt["receipt_path"]), receipt)
    atomic_json(STATE / "latest.json", receipt)
    return receipt


def ensure_terminal_fields(receipt: dict[str, Any]) -> None:
    receipt.setdefault("schema_version", SCHEMA_VERSION)
    receipt.setdefault("contract_version", CONTRACT_VERSION)
    receipt.setdefault("cycle_id", cycle_id())
    receipt.setdefault("stage", "verifier")
    receipt.setdefault("run_id", f"verifier-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    receipt.setdefault("trigger", os.environ.get("BUREAU_VERIFIER_TRIGGER", "chatgpt-quarter-past"))
    receipt.setdefault("started_at", utc_now())
    receipt.setdefault("receipt_path", str(RUNS / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{receipt['run_id']}.json"))
    receipt.setdefault("input_receipts", [])
    receipt.setdefault("findings", [])
    receipt.setdefault("metrics", {})
    receipt.setdefault("actions", [])
    receipt.setdefault("repaired_items", [])
    receipt.setdefault("evidence", [])
    receipt.setdefault("current_outcome_unknown_count", 0)
    receipt.setdefault("legacy_outcome_unavailable_count", 0)
    receipt.setdefault("sla_breach", False)
    receipt.setdefault("critical", False)
    receipt.setdefault("degraded", False)
    receipt.setdefault("next_action", "cycle health is current; dispatch may proceed when other gates allow it")
    receipt["lifecycle_state"] = "terminal"
    if receipt.get("result") not in TERMINAL_RESULTS:
        receipt["result"] = "completed"
    receipt["finished_at"] = utc_now()


def health_from_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    critical_findings = [f for f in receipt.get("findings", []) if isinstance(f, dict) and f.get("severity") == "critical"]
    circuit = receipt.get("circuit_breaker") if isinstance(receipt.get("circuit_breaker"), dict) else {}
    return {
        "cycle_id": receipt.get("cycle_id"),
        "updated_at": receipt.get("finished_at"),
        "critical_findings": critical_findings,
        "degraded": bool(receipt.get("degraded")),
        "critical": bool(receipt.get("critical")),
        "allow_next_dispatch": bool(circuit.get("allow_next_dispatch", not receipt.get("critical"))),
    }


def publish(receipt: dict[str, Any]) -> dict[str, Any]:
    ensure_terminal_fields(receipt)
    critical = any(isinstance(f, dict) and f.get("severity") == "critical" for f in receipt.get("findings", []))
    receipt["critical"] = bool(critical)
    receipt["degraded"] = bool(receipt.get("degraded") or critical or any(isinstance(f, dict) and f.get("severity") == "degraded" for f in receipt.get("findings", [])))
    receipt["circuit_breaker"] = {
        "active": bool(critical),
        "critical": bool(critical),
        "allow_next_dispatch": not bool(critical),
        "reason": "critical finding" if critical else None,
    }
    path = Path(str(receipt["receipt_path"])).expanduser()
    atomic_json(path, receipt)
    validation = helper(["validate", str(path), "--stage", "verifier", "--cycle-id", str(receipt.get("cycle_id"))])
    if validation["returncode"] != 0:
        receipt["result"] = "failed"
        receipt["critical"] = True
        receipt["degraded"] = True
        receipt["findings"].append(
            {
                "kind": "verifier_receipt_integrity",
                "severity": "critical",
                "detail": (validation.get("stderr") or validation.get("stdout") or "receipt validation failed")[:1000],
            }
        )
        receipt["circuit_breaker"] = {
            "active": True,
            "critical": True,
            "allow_next_dispatch": False,
            "reason": "verifier receipt failed installed helper validation",
        }
        receipt["finished_at"] = utc_now()
        atomic_json(path, receipt)
        validation = helper(["validate", str(path), "--stage", "verifier", "--cycle-id", str(receipt.get("cycle_id"))])
    atomic_json(STATE / "latest.json", receipt)
    atomic_json(HEALTH_PATH, health_from_receipt(receipt))
    return validation


def lease_path(lease_name: str) -> Path:
    return LEASES / f"{lease_name}.lock"


def acquire_lease(receipt: dict[str, Any]) -> tuple[Any | None, Path, dict[str, Any] | None]:
    LEASES.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"bureau-cycle:{receipt['cycle_id']}:verifier"
    path = lease_path(name)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        existing = handle.read(4096)
        handle.close()
        return None, path, {"lease": name, "path": str(path), "content": existing[:1000]}
    acquired = {
        "lease": name,
        "owner": receipt.get("run_id"),
        "pid": os.getpid(),
        "cycle_id": receipt.get("cycle_id"),
        "stage": "verifier",
        "acquired_at": utc_now(),
    }
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(acquired, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
    return handle, path, acquired


def release_lease(handle: Any, path: Path, receipt: dict[str, Any]) -> None:
    released = {
        "lease": f"bureau-cycle:{receipt.get('cycle_id')}:verifier",
        "released_by": receipt.get("run_id"),
        "released_at": utc_now(),
        "cycle_id": receipt.get("cycle_id"),
        "stage": "verifier",
    }
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(released, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def add_finding(receipt: dict[str, Any], kind: str, severity: str, detail: str, **extra: Any) -> None:
    item = {"kind": kind, "severity": severity, "detail": detail}
    item.update(extra)
    receipt.setdefault("findings", []).append(item)
    if severity in {"degraded", "critical"}:
        receipt["degraded"] = True
    if severity == "critical":
        receipt["critical"] = True


def validate_operator(receipt: dict[str, Any]) -> None:
    operator_latest = OPERATOR_STATE / "latest.json"
    operator = load_json(operator_latest)
    if operator_latest.exists():
        try:
            receipt.setdefault("input_receipts", []).append({"path": str(operator_latest), "sha256": sha256_file(operator_latest)})
        except OSError as exc:
            add_finding(receipt, "operator_receipt_hash_unavailable", "degraded", str(exc)[:500])
    if not isinstance(operator, dict):
        add_finding(receipt, "operator_receipt_missing", "degraded", str(operator_latest))
        receipt.setdefault("evidence", []).append({"kind": "operator_missing", "path": str(operator_latest)})
        return
    validation = helper(["validate", str(operator_latest), "--stage", "operator", "--cycle-id", str(receipt["cycle_id"])])
    valid = validation["returncode"] == 0 and isinstance(validation.get("json"), dict) and validation["json"].get("valid") is True
    receipt.setdefault("evidence", []).append(
        {
            "kind": "operator_latest",
            "path": str(operator_latest),
            "cycle_id": operator.get("cycle_id"),
            "result": operator.get("result"),
            "degraded": operator.get("degraded"),
            "lifecycle_state": operator.get("lifecycle_state"),
            "contract_version": operator.get("contract_version"),
            "schema_version": operator.get("schema_version"),
            "valid_same_cycle_terminal": valid,
        }
    )
    if not valid:
        detail = validation["json"].get("errors") if isinstance(validation.get("json"), dict) else (validation.get("stderr") or validation.get("stdout"))
        add_finding(receipt, "operator_receipt_invalid_or_not_same_cycle", "degraded", json.dumps(detail, ensure_ascii=False)[:1000])
    elif operator.get("degraded"):
        add_finding(receipt, "operator_receipt_degraded", "degraded", "operator terminal receipt is degraded")
    queue_changes = operator.get("queue_changes") if isinstance(operator.get("queue_changes"), list) else []
    if queue_changes and not operator.get("selected_task_id"):
        add_finding(
            receipt,
            "unbound_operator_mutation",
            "critical",
            "operator receipt contains queue_changes without selected_task_id binding",
            queue_change_count=len(queue_changes),
        )


def attention(receipt: dict[str, Any]) -> None:
    result = helper(["attention", "--task-db", str(TASK_DB), "--horizon-seconds", "10800"], timeout=60)
    data = result.get("json") if isinstance(result.get("json"), dict) else {}
    if result["returncode"] != 0 or not isinstance(data, dict) or data.get("available") is False:
        add_finding(receipt, "attention_unavailable", "degraded", (result.get("stderr") or result.get("stdout") or data.get("error") or "attention helper failed")[:1000])
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    current_unknown = int(counts.get("current_outcome_unknown") or 0)
    stale_running = int(counts.get("stale_running") or 0)
    recent_failed = int(counts.get("recent_failed") or 0)
    legacy_unknown = int(counts.get("legacy_outcome_unavailable") or 0)
    metrics = receipt.setdefault("metrics", {})
    metrics.update(
        {
            "current_attention_count": int(data.get("current_attention_count") or 0),
            "current_outcome_unknown_count": current_unknown,
            "legacy_outcome_unavailable_count": legacy_unknown,
            "recent_failed_count": recent_failed,
            "stale_running_count": stale_running,
            "healthy_running_count": int(counts.get("healthy_running") or 0),
            "historical_failed_count": int(counts.get("historical_failed") or 0),
            "terminal_history_count": int(counts.get("terminal_history") or 0),
        }
    )
    receipt["current_outcome_unknown_count"] = current_unknown
    receipt["legacy_outcome_unavailable_count"] = legacy_unknown
    receipt.setdefault("evidence", []).append({"kind": "attention_helper", "counts": metrics.copy(), "task_db": str(TASK_DB)})
    if current_unknown:
        add_finding(receipt, "current_outcome_unknown", "critical", "recent interrupted task has no trustworthy terminal outcome", count=current_unknown)
    if stale_running:
        add_finding(receipt, "stale_running_task", "critical", "task runtime deadline has passed without terminal outcome", count=stale_running)
    if recent_failed:
        add_finding(receipt, "recent_failed_task", "degraded", "recent failed task exists; terminal outcome is known but requires attention", count=recent_failed)


def sqlite_quick_check(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "ok": False, "error": "missing"}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            rows = [row[0] for row in connection.execute("PRAGMA quick_check").fetchall()]
        finally:
            connection.close()
        return {"path": str(path), "exists": True, "ok": rows == ["ok"], "result": rows[:5]}
    except Exception as exc:  # integrity/readability evidence, not crash
        return {"path": str(path), "exists": True, "ok": False, "error": str(exc)[:500]}


def database_integrity(receipt: dict[str, Any]) -> None:
    checks = [sqlite_quick_check(TASK_DB), sqlite_quick_check(BUREAU_DB)]
    receipt.setdefault("evidence", []).append({"kind": "sqlite_quick_check", "checks": checks})
    for check in checks:
        if not check.get("ok"):
            severity = "critical" if check.get("exists") else "degraded"
            add_finding(receipt, "database_integrity", severity, json.dumps(check, ensure_ascii=False)[:1000], path=check.get("path"))


def systemctl_lines(args: list[str]) -> list[str]:
    env = os.environ.copy()
    env.update({"HOME": str(HOME), "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"})
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    proc = subprocess.run(
        ["/usr/bin/systemctl", "--user", *args],
        cwd=str(HOME),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    if proc.returncode not in {0, 1}:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def service_hygiene(receipt: dict[str, Any]) -> None:
    lines = systemctl_lines(["--failed", "--plain", "--no-legend", "--no-pager"])
    transient: list[str] = []
    non_transient: list[str] = []
    for line in lines:
        unit = line.split()[0]
        if TRANSIENT_UNIT_RE.match(unit):
            transient.append(unit)
        elif unit.endswith(".service"):
            non_transient.append(unit)
    receipt.setdefault("metrics", {}).update(
        {
            "systemd_failed_transient_unit_count": len(transient),
            "systemd_failed_non_transient_unit_count": len(non_transient),
        }
    )
    receipt.setdefault("evidence", []).append(
        {
            "kind": "systemd_failed_units",
            "transient_count": len(transient),
            "non_transient_units": non_transient[:20],
            "interpretation": "transient grabowski task/job/worker failures are not current attention without task/outcome evidence",
        }
    )
    if non_transient:
        add_finding(receipt, "non_transient_failed_units", "degraded", "non-transient failed user services exist", units=non_transient[:20])


def control_path_drift(receipt: dict[str, Any]) -> None:
    try:
        audit = audit_cycle_deployment(
            manifest_path=HOME / ".local/share/bureau/deployment-manifest.json",
            unit_root=HOME / ".config/systemd/user",
            shim_root=HOME / ".local/libexec",
        )
    except CycleDeploymentError as exc:
        audit = {
            "status": "invalid",
            "read_only": True,
            "self_heal": False,
            "findings": [exc.finding()],
        }
    findings = audit.get("findings") if isinstance(audit.get("findings"), list) else []
    stages = audit.get("stages") if isinstance(audit.get("stages"), list) else []
    receipt.setdefault("metrics", {})["control_path_checked_count"] = len(stages) * 3
    receipt.setdefault("metrics", {})["control_path_drift_count"] = len(findings)
    receipt.setdefault("evidence", []).append(
        {
            "kind": "canonical_cycle_deployment_audit",
            "status": audit.get("status"),
            "read_only": audit.get("read_only"),
            "self_heal": audit.get("self_heal"),
            "canonical_root": audit.get("canonical_root"),
            "findings": findings[:20],
        }
    )
    if findings:
        add_finding(
            receipt,
            "control_path_drift",
            "degraded",
            "cycle deployment differs from the immutable Bureau release",
            drift=findings[:20],
        )



def lease_inventory(receipt: dict[str, Any]) -> None:
    items: list[dict[str, Any]] = []
    for path in sorted(LEASES.glob("*.lock"))[:100]:
        stat = path.stat()
        content = path.read_text(encoding="utf-8", errors="replace")[:500]
        items.append({"path": str(path), "mtime_unix": int(stat.st_mtime), "size": stat.st_size, "content_prefix": content})
    receipt.setdefault("metrics", {})["lease_file_count"] = len(items)
    receipt.setdefault("evidence", []).append({"kind": "lease_inventory", "leases": items[:20]})


def workspace_inventory(receipt: dict[str, Any]) -> None:
    repos = HOME / "repos"
    repo_count = 0
    worktree_count = 0
    dirty_unknown = 0
    sampled: list[dict[str, Any]] = []
    if repos.is_dir():
        for repo in sorted(p for p in repos.iterdir() if p.is_dir())[:80]:
            if not ((repo / ".git").exists()):
                continue
            repo_count += 1
            proc = subprocess.run(
                ["/usr/bin/git", "-C", str(repo), "worktree", "list", "--porcelain"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            if proc.returncode != 0:
                dirty_unknown += 1
                sampled.append({"repo": str(repo), "worktrees_known": False, "error": proc.stderr[-300:]})
                continue
            paths = [line.split(" ", 1)[1] for line in proc.stdout.splitlines() if line.startswith("worktree ")]
            worktree_count += len(paths)
            sampled.append({"repo": str(repo), "worktree_count": len(paths), "sample": paths[:5]})
    receipt.setdefault("metrics", {}).update(
        {"repo_root_count": repo_count, "git_worktree_count": worktree_count, "worktree_unknown_count": dirty_unknown}
    )
    receipt.setdefault("evidence", []).append({"kind": "workspace_worktree_inventory", "sampled": sampled[:20]})
    if dirty_unknown:
        add_finding(receipt, "worktree_inventory_partial", "degraded", "some worktrees could not be inspected read-only", count=dirty_unknown)


def final_result(receipt: dict[str, Any]) -> None:
    if receipt.get("result") in {"blocked", "failed"}:
        return
    if any(isinstance(f, dict) and f.get("severity") in {"critical", "degraded"} for f in receipt.get("findings", [])):
        receipt["result"] = "partial"
    else:
        receipt["result"] = "completed"
    if receipt.get("critical"):
        receipt["next_action"] = "hold dispatch until critical verifier findings are cleared"
    elif receipt.get("degraded"):
        receipt["next_action"] = "inspect degraded verifier findings before increasing dispatch pressure"
    else:
        receipt["next_action"] = "cycle health is current; dispatch may proceed when other gates allow it"


def run() -> int:
    for directory in (STATE, RUNS, LEASES, HEALTH_PATH.parent):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = begin_receipt()
    receipt.setdefault("input_receipts", [])
    receipt.setdefault("findings", [])
    receipt.setdefault("metrics", {})
    receipt.setdefault("actions", [])
    receipt.setdefault("repaired_items", [])
    receipt.setdefault("evidence", [])
    lease_handle = None
    lease_file = None
    validation: dict[str, Any] | None = None
    try:
        lease_handle, lease_file, lease_info = acquire_lease(receipt)
        if lease_handle is None:
            receipt["result"] = "blocked"
            receipt["degraded"] = True
            receipt["actions"].append({"kind": "lease", "result": "blocked", "lease": f"bureau-cycle:{receipt['cycle_id']}:verifier", "path": str(lease_file)})
            receipt["evidence"].append({"kind": "lease_busy", "lease": lease_info})
            receipt["next_action"] = "allow the existing verifier invocation to finish"
        else:
            receipt["actions"].append({"kind": "lease", "result": "acquired", "lease": lease_info.get("lease"), "path": str(lease_file)})
            validate_operator(receipt)
            attention(receipt)
            database_integrity(receipt)
            service_hygiene(receipt)
            control_path_drift(receipt)
            lease_inventory(receipt)
            workspace_inventory(receipt)
            final_result(receipt)
    except Exception as exc:
        receipt["result"] = "failed"
        add_finding(receipt, "verifier_runtime_exception", "critical", f"{type(exc).__name__}: {exc}"[:1000])
        receipt["next_action"] = "repair verifier runtime exception before trusting cycle health"
    finally:
        if lease_handle is not None and lease_file is not None:
            receipt.setdefault("actions", []).append({"kind": "lease", "result": "released", "lease": f"bureau-cycle:{receipt.get('cycle_id')}:verifier", "path": str(lease_file)})
        validation = publish(receipt)
        if lease_handle is not None and lease_file is not None:
            try:
                release_lease(lease_handle, lease_file, receipt)
            except Exception as exc:
                receipt["result"] = "failed"
                add_finding(receipt, "lease_release_failed", "critical", str(exc)[:500])
                validation = publish(receipt)
        print(
            json.dumps(
                {
                    "status": "ok" if receipt.get("result") != "failed" else "failed",
                    "cycle_id": receipt.get("cycle_id"),
                    "result": receipt.get("result"),
                    "degraded": receipt.get("degraded"),
                    "critical": receipt.get("critical"),
                    "validation_returncode": validation.get("returncode") if isinstance(validation, dict) else None,
                    "report": receipt.get("receipt_path"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
