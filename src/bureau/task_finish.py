"""Finish Bureau tasks from explicit merged pull-request evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"verified", "completed", "done", "closed", "cancelled", "superseded"}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")




def sha256_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evidence_source_ref(binding: dict[str, Any], evidence: dict[str, Any]) -> str:
    merge_commit = pr_merge_commit(evidence) or "unknown-merge"
    head_sha = pr_head_sha(evidence) or "unknown-head"
    return f"github-pr:{binding['repo']}#{binding['number']}@{head_sha}:{merge_commit}"


def evidence_is_ai_only(evidence: dict[str, Any] | None) -> bool:
    if not isinstance(evidence, dict):
        return False
    if not any(key in evidence for key in ("state", "merged", "mergedAt", "merged_at")):
        lowered_keys = {str(key).lower() for key in evidence}
        return any("ai" in key or "summary" in key or "llm" in key for key in lowered_keys)
    return False

def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def task_paths(root: Path) -> list[Path]:
    tasks = root / "registry" / "tasks"
    return sorted(tasks.glob("*.json")) if tasks.exists() else []


def task_binding(task: dict[str, Any]) -> dict[str, Any] | None:
    meta = task.get("metadata")
    if not isinstance(meta, dict):
        return None
    raw = meta.get("pr_completion") or meta.get("pr_closure")
    if not isinstance(raw, dict):
        return None
    repo = raw.get("repo") or raw.get("repository")
    number = raw.get("number", raw.get("pr", raw.get("pr_number")))
    if not isinstance(repo, str) or "/" not in repo:
        return None
    try:
        number_int = int(number)
    except (TypeError, ValueError):
        return None
    if number_int < 1:
        return None
    return {
        "repo": repo,
        "number": number_int,
        "head_sha": raw.get("head_sha") or raw.get("expected_head_sha"),
        "auto_verify": bool(raw.get("auto_verify", False)),
        "post_merge_required": bool(raw.get("post_merge_required", False)),
    }


def pr_is_merged(pr: dict[str, Any]) -> bool:
    return (
        pr.get("merged") is True
        or str(pr.get("state", "")).upper() == "MERGED"
        or bool(pr.get("mergedAt") or pr.get("merged_at"))
    )


def pr_head_sha(pr: dict[str, Any]) -> str | None:
    value = pr.get("headRefOid") or pr.get("head_sha")
    return value if isinstance(value, str) else None


def pr_merge_commit(pr: dict[str, Any]) -> str | None:
    value = pr.get("mergeCommit") or pr.get("merge_commit")
    if isinstance(value, dict):
        value = value.get("oid") or value.get("sha")
    return value if isinstance(value, str) else None


def load_evidence(directory: Path, repo: str, number: int) -> dict[str, Any] | None:
    safe_repo = repo.replace("/", "__")
    names = [safe_repo + "__" + str(number) + ".json", str(number) + ".json"]
    for name in names:
        path = directory / name
        if path.exists():
            return load_json(path)
    return None


def make_finding(
    root: Path,
    task_path: Path,
    task: dict[str, Any],
    binding: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    current_blockers: list[str] = []
    if str(task.get("state", "")).lower() in TERMINAL_STATES:
        current_blockers.append("task is already terminal")
    if evidence is None:
        current_blockers.append("merge evidence is missing")
    elif evidence_is_ai_only(evidence):
        current_blockers.append("AI or prose-only evidence is insufficient")
    elif not pr_is_merged(evidence):
        current_blockers.append("pull request is not merged")
    if binding["post_merge_required"]:
        current_blockers.append("post-merge validation is required")
    head_sha = pr_head_sha(evidence) if isinstance(evidence, dict) else None
    if binding["head_sha"] and head_sha != binding["head_sha"]:
        current_blockers.append("pull request head sha does not match")
    finding = {
        "task_id": task.get("id"),
        "task_path": str(task_path.relative_to(root)),
        "repo": binding["repo"],
        "pr_number": binding["number"],
        "ready": not current_blockers,
        "auto_verify": binding["auto_verify"],
        "blockers": current_blockers,
    }
    if not current_blockers:
        evidence_payload = {
            "source": "github_pull_request",
            "source_ref": evidence_source_ref(binding, evidence or {}),
            "repo": binding["repo"],
            "pr_number": binding["number"],
            "head_sha": head_sha,
            "merge_commit": pr_merge_commit(evidence or {}),
            "non_claims": [
                "merged PR evidence does not prove deployment",
                "merged PR evidence does not prove dependent tasks are complete",
                "merged PR evidence does not prove runtime correctness",
            ],
        }
        receipt = {
            "kind": "bureau.pr_completion_receipt",
            "schema_version": 2,
            "task_id": task.get("id"),
            "outcome": "completed-by-merged-pr",
            "evidence": evidence_payload,
        }
        evidence_payload["evidence_sha256"] = sha256_json(evidence_payload)
        receipt["receipt_id"] = (
            f"pr-completion:{binding['repo']}#{binding['number']}:"
            f"{head_sha or 'unknown-head'}"
        )
        receipt["receipt_sha256"] = sha256_json(receipt)
        finding["receipt"] = receipt
    return finding


def scan(root: Path, evidence_dir: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for task_path in task_paths(root):
        task = load_json(task_path)
        binding = task_binding(task)
        if binding is None:
            continue
        evidence = load_evidence(evidence_dir, binding["repo"], binding["number"])
        findings.append(make_finding(root, task_path, task, binding, evidence))
    return findings


def apply_ready(root: Path, findings: list[dict[str, Any]], observed_at: str) -> list[str]:
    changed: list[str] = []
    for finding in findings:
        if not finding.get("ready") or not finding.get("auto_verify"):
            continue
        path = root / str(finding["task_path"])
        task = load_json(path)
        if str(task.get("state", "")).lower() in TERMINAL_STATES:
            continue
        task["state"] = "verified"
        metadata = task.setdefault("metadata", {})
        verification = metadata.setdefault("verification", {})
        receipt = finding["receipt"]
        evidence = receipt.get("evidence", {})
        if not receipt.get("receipt_id") or not receipt.get("receipt_sha256"):
            continue
        if not isinstance(evidence, dict) or not evidence.get("evidence_sha256"):
            continue
        if not evidence.get("source") or not evidence.get("source_ref"):
            continue
        verification["pr_completion"] = receipt
        verification["receipt_id"] = receipt["receipt_id"]
        verification["receipt_sha256"] = receipt["receipt_sha256"]
        verification["source"] = evidence["source"]
        verification["source_ref"] = evidence["source_ref"]
        verification["evidence_sha256"] = evidence["evidence_sha256"]
        verification["verified_at"] = observed_at
        write_json(path, task)
        changed.append(str(path.relative_to(root)))
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    observed_at = now_utc()
    root = Path(args.root).resolve()
    findings = scan(root, Path(args.evidence_dir).resolve())
    changed = apply_ready(root, findings, observed_at) if args.apply else []
    result = {
        "schema_version": 1,
        "observed_at": observed_at,
        "findings": findings,
        "changed": changed,
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for finding in findings:
            state = "READY" if finding["ready"] else "BLOCKED"
            task_id = finding["task_id"]
            repo = finding["repo"]
            number = finding["pr_number"]
            print(f"{state} {task_id} {repo}#{number}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
