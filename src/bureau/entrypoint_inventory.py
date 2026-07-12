from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path
from typing import Any

TEXT_EXTENSIONS = {
    ".md",
    ".toml",
    ".yml",
    ".yaml",
    ".json",
    ".py",
    ".service",
    ".timer",
    ".sh",
    "",
}
EXCLUDED_DIRS = {".git", ".pytest_cache", ".ruff_cache", "__pycache__", ".venv"}
DOC_SEARCH_ROOTS = ("README.md", "Makefile", ".github", "docs", "ops", "tests")
DOC_REFERENCE_EXCLUDED_PREFIXES = ("docs/reports/",)
ENTRYPOINT_PROG_RE = re.compile(r"argparse\.ArgumentParser\(prog=\"([^\"]+)\"")
MAIN_GUARD = 'if __name__ == "__main__"'


def _read_pyproject_scripts(root: Path) -> dict[str, str]:
    scripts: dict[str, str] = {}
    in_scripts = False
    for raw_line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_scripts = line == "[project.scripts]"
            continue
        if not in_scripts or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip().strip('"')
        value = value.strip().strip('"')
        if name and value:
            scripts[name] = value
    return dict(sorted(scripts.items()))


def _iter_text_files(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in sorted(root.rglob("*")):
        rel_parts = set(path.relative_to(root).parts)
        if rel_parts & EXCLUDED_DIRS:
            continue
        if not path.is_file():
            continue
        if path.suffix not in TEXT_EXTENSIONS:
            continue
        result.append(path)
    return result


def _doc_search_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for item in DOC_SEARCH_ROOTS:
        path = root / item
        if not path.exists():
            continue
        if path.is_file():
            files.append(path)
        else:
            files.extend(_iter_text_files(path))
    filtered: list[Path] = []
    for file in sorted(set(files)):
        rel = file.relative_to(root).as_posix()
        if any(rel.startswith(prefix) for prefix in DOC_REFERENCE_EXCLUDED_PREFIXES):
            continue
        filtered.append(file)
    return filtered


def _reference_files(path: Path, root: Path, tokens: list[str]) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    if any(token in line for token in tokens for line in lines):
        return [path.relative_to(root).as_posix()]
    return []

def _infer_layer(name: str, target: str) -> str:
    if name == "bureau":
        return "core_cli"
    if name in {"bureau-agent-frontier", "bureau-agent-scout"}:
        return "ops_frontier"
    if name.startswith("bureau-closure") or name == "bureau-pr-task-finish":
        return "ops_closure"
    if name == "bureau-status-capsule":
        return "ops_readonly_status"
    if name in {"bureau-codex-bridge", "bureau-review-steward", "bureau-source-pr-bridge"}:
        return "ops_bridge"
    if name == "bureau-gemini-preflight":
        return "ops_external_preflight"
    if target.startswith("bureau."):
        return "ops_or_auxiliary"
    return "unknown"


def _discover_module_entrypoints(root: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in sorted((root / "src/bureau").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if MAIN_GUARD not in text and "def main(" not in text:
            continue
        rel = path.relative_to(root).as_posix()
        module = "bureau." + path.stem
        prog_match = ENTRYPOINT_PROG_RE.search(text)
        results.append(
            {
                "module": module,
                "path": rel,
                "has_main_guard": MAIN_GUARD in text,
                "has_main_function": "def main(" in text,
                "argparse_prog": prog_match.group(1) if prog_match else None,
            }
        )
    return results


def _read_systemd_units(root: Path, scripts: dict[str, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for service in sorted((root / "ops/systemd").glob("*.service")):
        exec_start = None
        read_write_paths: list[str] = []
        after: list[str] = []
        for line in service.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart="):
                exec_start = line.removeprefix("ExecStart=")
            if line.startswith("ReadWritePaths="):
                read_write_paths.append(line.removeprefix("ReadWritePaths="))
            if line.startswith("After="):
                after.extend(line.removeprefix("After=").split())
        command = None
        args: list[str] = []
        basename = None
        matched_script = None
        if exec_start:
            parts = shlex.split(exec_start)
            if parts:
                command = parts[0]
                args = parts[1:]
                basename = Path(command).name
                if basename in scripts:
                    matched_script = basename
                elif basename == "bureau":
                    matched_script = "bureau"
        timer = service.with_suffix(".timer")
        result.append(
            {
                "unit": service.relative_to(root).as_posix(),
                "timer": timer.relative_to(root).as_posix() if timer.exists() else None,
                "exec_start": exec_start,
                "command": command,
                "args": args,
                "command_basename": basename,
                "matched_console_script": matched_script,
                "read_write_paths": read_write_paths,
                "after": after,
            }
        )
    return result


def build_inventory(root: Path) -> dict[str, Any]:
    root = root.resolve()
    scripts = _read_pyproject_scripts(root)
    doc_files = _doc_search_files(root)
    console_entries = []
    for name, target in scripts.items():
        refs: list[str] = []
        for file in doc_files:
            refs.extend(_reference_files(file, root, [name]))
        refs = sorted(set(refs))
        console_entries.append(
            {
                "name": name,
                "target": target,
                "layer": _infer_layer(name, target),
                "documentation_references": refs,
            }
        )
    systemd_units = _read_systemd_units(root, scripts)
    module_entries = _discover_module_entrypoints(root)
    return {
        "schema_version": 1,
        "kind": "bureau_console_entrypoint_inventory",
        "source": "src/bureau/entrypoint_inventory.py",
        "summary": {
            "packaged_console_scripts": len(console_entries),
            "module_entrypoints": len(module_entries),
            "systemd_services": len(systemd_units),
            "systemd_timers": sum(1 for item in systemd_units if item.get("timer")),
        },
        "console_scripts": console_entries,
        "module_entrypoints": module_entries,
        "systemd_units": systemd_units,
        "does_not_establish": [
            "runtime_units_installed",
            "hidden_external_users_absent",
            "safe_to_remove_entrypoints",
            "packaging_decision",
            "systemd_runtime_correctness",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bureau-entrypoint-inventory")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    inventory = build_inventory(Path(args.root))
    if args.json:
        print(json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "Bureau entrypoint inventory: "
            f"{inventory['summary']['packaged_console_scripts']} console scripts, "
            f"{inventory['summary']['systemd_services']} systemd services"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
