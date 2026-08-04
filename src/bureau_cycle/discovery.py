from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .cycle_contract import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    atomic_json,
    cycle_id,
    utc_now,
    validate_receipt,
)

STATE = Path(
    os.environ.get(
        "BUREAU_DISCOVERY_STATE_ROOT",
        Path.home() / ".local/state/bureau-halfhour-operator",
    )
).expanduser()
REGISTRY = Path(
    os.environ.get("BUREAU_DISCOVERY_REGISTRY", STATE / "source-registry.json")
).expanduser()
SOURCE_STATE = STATE / "source-state.json"
RUNS = STATE / "runs"
INBOX = STATE / "inbox"
LOCK = STATE / "scanner.lock"
MAX_NEW_CANDIDATES = 20
MAX_DOCUMENTS = 500
MAX_DOCUMENT_BYTES = 2_000_000
ACTIVE_STATES = {"open", "partial", "blocked", "planned", "in progress", "in-progress", "in arbeit"}
SECTION_MARKERS = (
    "offen",
    "nächste schritte",
    "naechste schritte",
    "next steps",
    "folgearbeiten",
    "backlog",
    "blocked",
)
SKIP_PARTS = {
    ".git",
    ".obsidian",
    ".smart-env",
    ".trash",
    "halde",
    "archive",
    "node_modules",
    "dist",
    "build",
    "target",
}
STALE_MARKERS = (" old", "alt", "copy", "kopie", "legacy", "deprecated")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_hash(*parts: str) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def git(repo: Path, *arguments: str, binary: bool = False) -> bytes | str:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "diff.external=",
            "-c",
            "core.attributesFile=/dev/null",
            "-C",
            str(repo),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=not binary,
        timeout=20,
        env=environment,
    )
    if result.returncode:
        stderr = result.stderr.decode("utf-8", "replace") if binary else result.stderr
        raise RuntimeError(stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


def resolve_commit(repo: Path, configured_ref: str | None = None) -> tuple[str, str]:
    refs = (configured_ref,) if configured_ref else ("origin/main", "main")
    for ref in refs:
        if not ref or ref.startswith("-") or any(value in ref for value in (" ", "\t", "\n", ":")):
            raise RuntimeError(f"unsafe source ref: {ref!r}")
        try:
            commit = str(git(repo, "rev-parse", f"{ref}^{{commit}}")).strip()
        except RuntimeError:
            continue
        if re.fullmatch(r"[0-9a-f]{40}", commit):
            return ref, commit
    raise RuntimeError("no usable commit found")


def safe_git_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe repository path: {value}")
    return path.as_posix()


def read_blob(repo: Path, commit: str, relative: str) -> bytes | None:
    relative = safe_git_path(relative)
    specification = f"{commit}:{relative}"
    try:
        git(repo, "cat-file", "-e", specification)
    except RuntimeError:
        return None
    value = git(repo, "show", specification, binary=True)
    assert isinstance(value, bytes)
    if len(value) > MAX_DOCUMENT_BYTES:
        raise RuntimeError(f"document exceeds {MAX_DOCUMENT_BYTES} bytes")
    return value


def candidate(
    *,
    source_id: str,
    revision: str,
    path: str,
    anchor: str,
    project: str,
    kind: str,
    summary: str,
    status: str,
    external_id: str | None = None,
    confidence: str = "medium",
) -> dict[str, Any]:
    normalized = " ".join(summary.split()).strip()
    target = normalized.casefold()
    local_id = external_id or anchor
    fingerprint = canonical_hash(source_id, path, local_id, target)
    return {
        "fingerprint": fingerprint,
        "source_id": source_id,
        "source_revision": revision,
        "source_path": path,
        "source_anchor": anchor,
        "project": project,
        "candidate_kind": kind,
        "external_id": external_id,
        "status": status,
        "summary": normalized[:1000],
        "target_outcome": normalized[:1000],
        "confidence": confidence,
    }


def extract_json(
    source_id: str, revision: str, path: str, project: str, text: str
) -> list[dict[str, Any]]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    tasks = (
        value
        if isinstance(value, list)
        else value.get("tasks", [])
        if isinstance(value, dict)
        else []
    )
    if not isinstance(tasks, list):
        return []
    found: list[dict[str, Any]] = []
    for index, item in enumerate(tasks):
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or item.get("state") or "").strip().casefold()
        if status not in ACTIVE_STATES:
            continue
        identifier = item.get("id") or item.get("task_id")
        title = item.get("title") or item.get("summary") or item.get("goal")
        if not isinstance(title, str) or not title.strip():
            continue
        found.append(
            candidate(
                source_id=source_id,
                revision=revision,
                path=path,
                anchor=f"item:{identifier or index}",
                project=project,
                kind="structured-task",
                summary=title,
                status=status,
                external_id=str(identifier) if identifier else None,
                confidence="high",
            )
        )
    return found


def extract_markdown(
    source_id: str, revision: str, path: str, project: str, text: str
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    active_section = False
    first_heading = Path(path).stem
    doc_status = ""
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        if heading:
            title = heading.group(1).strip()
            if first_heading == Path(path).stem:
                first_heading = title
            active_section = any(marker in title.casefold() for marker in SECTION_MARKERS)
            continue
        status_match = re.match(r"^(?:status|state)\s*:\s*(.+?)\s*$", stripped, re.IGNORECASE)
        if status_match:
            state = status_match.group(1).strip().casefold().strip("🟡🟢🔴 ")
            if state in ACTIVE_STATES:
                doc_status = state
            continue
        checklist = re.match(r"^[-*+]\s+\[\s\]\s+(.+?)\s*$", stripped)
        if checklist:
            found.append(
                candidate(
                    source_id=source_id,
                    revision=revision,
                    path=path,
                    anchor=f"L{number}",
                    project=project,
                    kind="unchecked-item",
                    summary=checklist.group(1),
                    status=doc_status or "open",
                    confidence="high",
                )
            )
            continue
        if active_section:
            bullet = re.match(r"^(?:[-*+]|\d+[.)])\s+(.+?)\s*$", stripped)
            if bullet and not re.match(r"^\[[xX]\]", bullet.group(1)):
                summary = bullet.group(1).strip()
                if len(summary) >= 8:
                    found.append(
                        candidate(
                            source_id=source_id,
                            revision=revision,
                            path=path,
                            anchor=f"L{number}",
                            project=project,
                            kind="planning-item",
                            summary=summary,
                            status=doc_status or "open",
                            confidence="medium",
                        )
                    )
    if doc_status and not found:
        found.append(
            candidate(
                source_id=source_id,
                revision=revision,
                path=path,
                anchor="document-status",
                project=project,
                kind="active-planning-document",
                summary=first_heading,
                status=doc_status,
                confidence="low",
            )
        )
    return found


def extract(
    source_id: str, revision: str, path: str, project: str, payload: bytes
) -> list[dict[str, Any]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    if path.casefold().endswith(".json"):
        return extract_json(source_id, revision, path, project, text)
    if path.casefold().endswith((".md", ".markdown")):
        return extract_markdown(source_id, revision, path, project, text)
    return []


def vault_paths(
    registry: dict[str, Any], vault_root: Path, commit: str
) -> list[tuple[str, list[str]]]:
    mapping: dict[str, set[str]] = {}
    for repo in registry.get("repositories", []):
        if not repo.get("enabled"):
            continue
        for absolute in repo.get("vault_paths", []):
            try:
                relative = Path(absolute).resolve().relative_to(vault_root.resolve()).as_posix()
            except (ValueError, OSError):
                continue
            mapping.setdefault(relative, set()).add(str(repo.get("name")))
    result: list[tuple[str, list[str]]] = []
    markers = tuple(str(item).casefold() for item in registry.get("filename_markers", []))
    for root, projects in sorted(mapping.items()):
        try:
            raw = str(git(vault_root, "ls-tree", "-r", "--name-only", commit, "--", root))
        except RuntimeError:
            continue
        for path in raw.splitlines():
            parts = PurePosixPath(path).parts
            lower = path.casefold()
            if any(part in SKIP_PARTS for part in parts):
                continue
            if not lower.endswith((".md", ".json")):
                continue
            name = PurePosixPath(path).name.casefold()
            if not any(marker in name for marker in markers):
                continue
            if any(marker in f" {name}" for marker in STALE_MARKERS):
                continue
            result.append((path, sorted(projects)))
    return result


def main() -> int:
    started_monotonic = time.monotonic()
    started_at = utc_now()
    cycle = cycle_id()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"scanner-{stamp}"
    report_path = RUNS / f"{stamp}.json"
    for directory in (STATE, RUNS, INBOX):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_handle = LOCK.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        now = utc_now()
        blocked = {
            "schema_version": SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "cycle_id": cycle,
            "stage": "scanner",
            "run_id": run_id,
            "scanner_run_id": run_id,
            "trigger": "local-half-hour",
            "schedule_role": "deterministic-discovery-scanner",
            "started_at": started_at,
            "finished_at": now,
            "lifecycle_state": "terminal",
            "result": "blocked",
            "degraded": False,
            "baseline": not SOURCE_STATE.exists(),
            "promotion_allowed": False,
            "source_revisions": [],
            "changed_documents": [],
            "new_candidates": [],
            "resolved_candidate_fingerprints": [],
            "scanner_errors": [],
            "overflow_candidate_count": 0,
            "metrics": {},
            "receipt_path": str(report_path),
            "evidence": [{"kind": "lock", "value": "scanner-already-running"}],
            "next_action": "allow the existing scanner invocation to finish",
        }
        errors = validate_receipt(blocked, expected_stage="scanner", expected_cycle_id=cycle)
        if errors:
            raise RuntimeError(
                "blocked receipt contract failed: " + "; ".join(errors)
            ) from None
        atomic_json(report_path, blocked)
        atomic_json(STATE / "latest.json", blocked)
        atomic_json(INBOX / f"{cycle}-{stamp}-blocked.json", blocked)
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "scanner-already-running",
                    "cycle_id": cycle,
                    "report": str(report_path),
                }
            )
        )
        return 0

    running = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": cycle,
        "stage": "scanner",
        "run_id": run_id,
        "scanner_run_id": run_id,
        "trigger": "local-half-hour",
        "schedule_role": "deterministic-discovery-scanner",
        "started_at": started_at,
        "finished_at": None,
        "lifecycle_state": "running",
        "result": None,
        "degraded": False,
        "baseline": not SOURCE_STATE.exists(),
        "promotion_allowed": False,
        "source_revisions": [],
        "receipt_path": str(report_path),
        "evidence": [],
        "next_action": "finish the commit-bound discovery scan",
    }
    errors = validate_receipt(
        running,
        expected_stage="scanner",
        expected_cycle_id=cycle,
        require_terminal=False,
    )
    if errors:
        raise RuntimeError("running receipt contract failed: " + "; ".join(errors))
    atomic_json(report_path, running)
    atomic_json(STATE / "latest.json", running)

    try:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"missing or invalid source registry: {REGISTRY}") from exc
    if not isinstance(registry, dict):
        raise RuntimeError(f"missing or invalid source registry: {REGISTRY}")
    baseline = not SOURCE_STATE.exists()
    if baseline:
        previous = {"documents": {}, "candidate_fingerprints": []}
    else:
        try:
            previous = json.loads(SOURCE_STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid source state: {SOURCE_STATE}") from exc
        if not isinstance(previous, dict):
            raise RuntimeError(f"invalid source state: {SOURCE_STATE}")
    previous_documents = previous.get("documents", {}) if isinstance(previous, dict) else {}
    current_documents: dict[str, Any] = {}
    source_revisions: list[dict[str, Any]] = []
    changed_documents: list[dict[str, Any]] = []
    scanner_errors: list[dict[str, str]] = []
    documents_considered = 0

    def process_document(
        source_id: str, revision: str, repo: Path, path: str, project: str
    ) -> None:
        nonlocal documents_considered
        if documents_considered >= MAX_DOCUMENTS:
            return
        documents_considered += 1
        key = f"{source_id}:{path}"
        try:
            payload = read_blob(repo, revision, path)
            if payload is None:
                if key in previous_documents:
                    changed_documents.append(
                        {"source_id": source_id, "source_path": path, "change": "deleted"}
                    )
                return
            digest = sha256_bytes(payload)
            prior = previous_documents.get(key, {})
            if prior.get("sha256") == digest:
                refreshed = dict(prior)
                refreshed["source_revision"] = revision
                refreshed_candidates = []
                for item in prior.get("candidates", []):
                    candidate_item = dict(item)
                    candidate_item["source_revision"] = revision
                    refreshed_candidates.append(candidate_item)
                refreshed["candidates"] = refreshed_candidates
                current_documents[key] = refreshed
                return
            candidates = extract(source_id, revision, path, project, payload)
            current_documents[key] = {
                "source_id": source_id,
                "source_revision": revision,
                "source_path": path,
                "project": project,
                "sha256": digest,
                "candidates": candidates,
            }
            changed_documents.append(
                {
                    "source_id": source_id,
                    "source_revision": revision,
                    "source_path": path,
                    "project": project,
                    "change": "new" if not prior else "modified",
                    "sha256": digest,
                    "candidate_count": len(candidates),
                }
            )
        except Exception as exc:
            scanner_errors.append(
                {"source_id": source_id, "source_path": path, "error": str(exc)[:1000]}
            )

    enabled_repositories = [
        item for item in registry.get("repositories", []) if item.get("enabled")
    ]
    for item in enabled_repositories:
        root = Path(str(item.get("root", ""))).expanduser()
        source_id = str(item.get("source_id") or f"repo:{item.get('name')}")
        project = str(item.get("name") or source_id)
        try:
            if not (root / ".git").exists():
                raise RuntimeError("canonical repository checkout is missing")
            ref, commit = resolve_commit(root, str(item.get("ref") or "origin/main"))
            source_revisions.append(
                {"source_id": source_id, "root": str(root), "ref": ref, "revision": commit}
            )
            for relative in item.get("planning_files", []):
                process_document(source_id, commit, root, str(relative), project)
        except Exception as exc:
            scanner_errors.append(
                {"source_id": source_id, "source_path": str(root), "error": str(exc)[:1000]}
            )

    vault_root = Path(str(registry.get("vault_root", ""))).expanduser()
    try:
        vault_ref, vault_commit = resolve_commit(
            vault_root, str(registry.get("vault_ref") or "origin/main")
        )
        source_revisions.append(
            {
                "source_id": "vault:gewebe",
                "root": str(vault_root),
                "ref": vault_ref,
                "revision": vault_commit,
            }
        )
        for path, projects in vault_paths(registry, vault_root, vault_commit):
            process_document("vault:gewebe", vault_commit, vault_root, path, ",".join(projects))
    except Exception as exc:
        scanner_errors.append(
            {"source_id": "vault:gewebe", "source_path": str(vault_root), "error": str(exc)[:1000]}
        )

    for key, prior in previous_documents.items():
        if key not in current_documents and not any(
            item.get("source_id") == prior.get("source_id")
            and item.get("source_path") == prior.get("source_path")
            for item in changed_documents
        ):
            current_documents[key] = prior

    all_candidates: dict[str, dict[str, Any]] = {}
    for document in current_documents.values():
        for item in document.get("candidates", []):
            all_candidates[item["fingerprint"]] = item
    previous_fingerprints = (
        set(previous.get("candidate_fingerprints", [])) if isinstance(previous, dict) else set()
    )
    current_fingerprints = set(all_candidates)
    new_fingerprints = sorted(current_fingerprints - previous_fingerprints)
    resolved_fingerprints = sorted(previous_fingerprints - current_fingerprints)
    new_candidates_all = [all_candidates[value] for value in new_fingerprints]
    overflow = max(0, len(new_candidates_all) - MAX_NEW_CANDIDATES)
    new_candidates = new_candidates_all[:MAX_NEW_CANDIDATES]
    errors_truncated = scanner_errors[:50]
    promotion_allowed = (
        not baseline
        and not errors_truncated
        and overflow == 0
        and documents_considered < MAX_DOCUMENTS
    )
    if errors_truncated or overflow or documents_considered >= MAX_DOCUMENTS:
        result = "partial"
    elif changed_documents or new_candidates or resolved_fingerprints:
        result = "completed"
    else:
        result = "idle"

    finished_at = utc_now()
    report = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "cycle_id": cycle,
        "stage": "scanner",
        "run_id": run_id,
        "scanner_run_id": run_id,
        "trigger": "local-half-hour",
        "schedule_role": "deterministic-discovery-scanner",
        "started_at": started_at,
        "finished_at": finished_at,
        "lifecycle_state": "terminal",
        "duration_ms": round((time.monotonic() - started_monotonic) * 1000),
        "result": result,
        "degraded": result in {"partial", "failed"},
        "baseline": baseline,
        "promotion_allowed": promotion_allowed,
        "source_registry_sha256": sha256_bytes(REGISTRY.read_bytes()),
        "source_revisions": source_revisions,
        "changed_documents": changed_documents[:200],
        "new_candidates": new_candidates,
        "unchanged_candidate_count": len(current_fingerprints & previous_fingerprints),
        "resolved_candidate_fingerprints": resolved_fingerprints[:200],
        "scanner_errors": errors_truncated,
        "overflow_candidate_count": overflow,
        "metrics": {
            "enabled_repository_count": len(enabled_repositories),
            "source_revision_count": len(source_revisions),
            "documents_considered": documents_considered,
            "documents_changed": len(changed_documents),
            "candidate_count": len(current_fingerprints),
            "new_candidate_count": len(new_fingerprints),
            "resolved_candidate_count": len(resolved_fingerprints),
            "scanner_error_count": len(scanner_errors),
        },
        "receipt_path": str(report_path),
        "evidence": [
            {
                "kind": "source-registry-sha256",
                "value": sha256_bytes(REGISTRY.read_bytes()),
            },
            {
                "kind": "source-revision-count",
                "value": len(source_revisions),
            },
        ],
        "next_action": "curator may evaluate candidates"
        if promotion_allowed
        else "curator must not promote; inspect scanner limits or errors",
    }
    errors = validate_receipt(report, expected_stage="scanner", expected_cycle_id=cycle)
    if errors:
        raise ValueError("receipt contract failed")
    atomic_json(report_path, report)
    atomic_json(STATE / "latest.json", report)
    atomic_json(INBOX / f"{cycle}-{stamp}.json", report)
    atomic_json(
        SOURCE_STATE,
        {
            "schema_version": 2,
            "contract_version": CONTRACT_VERSION,
            "updated_at": finished_at,
            "source_revisions": source_revisions,
            "documents": current_documents,
            "candidate_fingerprints": sorted(current_fingerprints),
        },
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "cycle_id": cycle,
                "run_id": run_id,
                "result": result,
                "changed_documents": len(changed_documents),
                "new_candidates": len(new_candidates),
                "report": str(report_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)[:2000]}), file=sys.stderr)
        raise
