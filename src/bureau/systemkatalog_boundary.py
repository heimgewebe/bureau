from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

RETIRED_MODULES = (
    "src/bureau/cabinet_bridge.py",
    "src/bureau/cabinet_bridge_import_policy.py",
    "src/bureau/cabinet_bridge_preview.py",
    "src/bureau/cabinet_bridge_receipt.py",
    "src/bureau/cabinet_bridge_review.py",
    "src/bureau/cabinet_frontier_reader.py",
    "src/bureau/cabinet_graph.py",
    "src/bureau/cabinet_promotion_write.py",
)
RETIRED_CLI_COMMANDS = (
    "systemkatalog-graph",
    "systemkatalog-frontier",
    "systemkatalog-bridge-probe",
    "systemkatalog-promote",
    "systemkatalog-validate-task",
    "systemkatalog-import-preview",
    "systemkatalog-import-reviewed",
)
RETIRED_CONSOLE_SCRIPTS = (
    "bureau-systemkatalog-bridge-preview",
    "bureau-systemkatalog-bridge-review",
    "bureau-systemkatalog-bridge-receipt",
    "bureau-systemkatalog-bridge-import-policy",
    "bureau-systemkatalog-frontier-reader",
)
RETIRED_DEFAULT_PATH_FRAGMENTS = (
    "repos/cabinet/steuerung/10 Lage/ecosystem-graph.json",
    "repos/cabinet/registry/ecosystem/bureau-bridge.json",
)
ARCHIVED_MARKDOWN = "docs/archive/cabinet-era/cabinet-bridge-import-review-contract-v0.md"
ARCHIVED_POLICY = (
    "docs/archive/cabinet-era/cabinet-bridge-import-review-contract-v0.policy.json"
)
MARKDOWN_POINTER = "docs/cabinet-bridge-import-review-contract-v0.md"
POLICY_POINTER = "docs/cabinet-bridge-import-review-contract-v0.policy.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project_scripts(path: Path) -> dict[str, str]:
    scripts: dict[str, str] = {}
    in_scripts = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_scripts = line == "[project.scripts]"
            continue
        if in_scripts and "=" in line:
            name, value = line.split("=", 1)
            scripts[name.strip().strip('"')] = value.strip().strip('"')
    return scripts


def _active_text_paths(root: Path) -> list[Path]:
    paths = [root / "pyproject.toml", root / "Makefile", root / "README.md"]
    paths.extend((root / "src/bureau").glob("*.py"))
    paths.extend((root / "ops/systemd").glob("*"))
    paths.append(root / "docs/operations.md")
    boundary = root / "src/bureau/systemkatalog_boundary.py"
    return sorted(path for path in paths if path.is_file() and path != boundary)


def validate_boundary(root: Path) -> dict[str, Any]:
    root = root.resolve()
    violations: list[dict[str, str]] = []

    for relative in RETIRED_MODULES:
        if (root / relative).exists():
            violations.append({"kind": "retired_module_present", "path": relative})

    cli_path = root / "src/bureau/cli.py"
    cli_text = cli_path.read_text(encoding="utf-8")
    for command in RETIRED_CLI_COMMANDS:
        if f'"{command}"' in cli_text or f"'{command}'" in cli_text:
            violations.append({"kind": "retired_cli_command_present", "value": command})

    scripts = _project_scripts(root / "pyproject.toml")
    for name in RETIRED_CONSOLE_SCRIPTS:
        if name in scripts:
            violations.append({"kind": "retired_console_script_present", "value": name})
    for name, target in scripts.items():
        if target.startswith("bureau.cabinet_"):
            violations.append(
                {
                    "kind": "retired_console_target_present",
                    "value": f"{name}={target}",
                }
            )

    for path in _active_text_paths(root):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(root).as_posix()
        if path != cli_path:
            for command in RETIRED_CLI_COMMANDS:
                if command in text:
                    violations.append(
                        {
                            "kind": "retired_cli_reference_present",
                            "path": relative,
                            "value": command,
                        }
                    )
        if path != root / "pyproject.toml":
            for name in RETIRED_CONSOLE_SCRIPTS:
                if name in text:
                    violations.append(
                        {
                            "kind": "retired_console_reference_present",
                            "path": relative,
                            "value": name,
                        }
                    )
        for fragment in RETIRED_DEFAULT_PATH_FRAGMENTS:
            if fragment in text:
                violations.append(
                    {
                        "kind": "retired_default_path_present",
                        "path": relative,
                        "value": fragment,
                    }
                )
        if "from .cabinet_" in text or "bureau.cabinet_" in text:
            violations.append(
                {"kind": "retired_cabinet_import_present", "path": relative}
            )

    archive_md = root / ARCHIVED_MARKDOWN
    archive_policy = root / ARCHIVED_POLICY
    pointer_md = root / MARKDOWN_POINTER
    pointer_policy = root / POLICY_POINTER
    for path, kind in (
        (archive_md, "archived_markdown_missing"),
        (archive_policy, "archived_policy_missing"),
        (pointer_md, "markdown_pointer_missing"),
        (pointer_policy, "policy_pointer_missing"),
    ):
        if not path.is_file():
            violations.append({"kind": kind, "path": path.relative_to(root).as_posix()})

    if archive_md.is_file() and pointer_md.is_file():
        pointer_text = pointer_md.read_text(encoding="utf-8")
        expected_hash = _sha256(archive_md)
        if "Status: **retired**" not in pointer_text:
            violations.append({"kind": "markdown_pointer_not_retired"})
        if ARCHIVED_MARKDOWN not in pointer_text or expected_hash not in pointer_text:
            violations.append({"kind": "markdown_pointer_archive_mismatch"})
        archive_text = archive_md.read_text(encoding="utf-8")
        for marker in (
            "importReviewRequired == true",
            "importAllowed == false",
            "cabinet-ci-review-gate",
        ):
            if marker not in archive_text:
                violations.append(
                    {"kind": "archived_markdown_incomplete", "value": marker}
                )

    if archive_policy.is_file() and pointer_policy.is_file():
        try:
            pointer = json.loads(pointer_policy.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            violations.append({"kind": "policy_pointer_invalid_json", "value": str(exc)})
        else:
            if pointer.get("status") != "retired":
                violations.append({"kind": "policy_pointer_not_retired"})
            if pointer.get("retired_by") != "OPERATOR-ECOSYSTEM-REDUNDANCY-V1-T024":
                violations.append({"kind": "policy_pointer_task_mismatch"})
            if pointer.get("archive") != ARCHIVED_POLICY:
                violations.append({"kind": "policy_pointer_archive_mismatch"})
            if pointer.get("archive_sha256") != _sha256(archive_policy):
                violations.append({"kind": "policy_pointer_hash_mismatch"})
        try:
            archived = json.loads(archive_policy.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            violations.append({"kind": "archived_policy_invalid_json", "value": str(exc)})
        else:
            if archived.get("kind") != "bureau.cabinet_bridge_import_review_contract_policy":
                violations.append({"kind": "archived_policy_kind_mismatch"})

    return {
        "schema_version": 1,
        "kind": "bureau_systemkatalog_boundary_report",
        "status": "pass" if not violations else "fail",
        "violations": violations,
        "checked": {
            "retired_modules": len(RETIRED_MODULES),
            "retired_cli_commands": len(RETIRED_CLI_COMMANDS),
            "retired_console_scripts": len(RETIRED_CONSOLE_SCRIPTS),
            "active_text_files": len(_active_text_paths(root)),
        },
        "does_not_establish": [
            "absence of historical references outside active source and configuration",
            "runtime deployment state",
            "test sufficiency beyond the executed repository gates",
            "merge readiness",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bureau-static-catalog-boundary")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = validate_boundary(Path(args.root))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Bureau Systemkatalog boundary: {report['status']}")
        for violation in report["violations"]:
            print(json.dumps(violation, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
