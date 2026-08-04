"""Read-only provenance audit for the Bureau cycle scheduler deployment."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import stat
from pathlib import Path
from typing import Any

from .runtime_identity import _package_tree_sha256

SCHEMA_VERSION = 1
MAX_JSON_BYTES = 256 * 1024
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA64 = re.compile(r"^[0-9a-f]{64}$")
STAGES = (
    ("discovery", "bureau-halfhour-operator", "bureau_cycle.discovery_runner"),
    ("curator", "bureau-curator", "bureau_cycle.curator_runner"),
    ("operator", "bureau-operator-control", "bureau_cycle.operator_runner"),
    ("verifier", "bureau-verifier-control", "bureau_cycle.verifier_runner"),
    ("closure", "bureau-closure-planner", "bureau.closure_runner"),
)
SOURCES = (
    "src/bureau_cycle/__init__.py",
    "src/bureau_cycle/common.py",
    "src/bureau_cycle/cycle_contract.py",
    "src/bureau_cycle/discovery.py",
    "src/bureau_cycle/discovery_runner.py",
    "src/bureau_cycle/curator_runner.py",
    "src/bureau_cycle/operator_runner.py",
    "src/bureau_cycle/verifier_runner.py",
    "src/bureau/cli.py",
    "src/bureau/closure_runner.py",
    "src/bureau/cycle_deployment.py",
    "src/bureau/cycle_stage.py",
    "src/bureau/runtime_identity.py",
)


class CycleDeploymentError(RuntimeError):
    def __init__(self, code: str, message: str, path: Path | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path

    def finding(self) -> dict[str, str]:
        item = {"code": self.code, "message": self.message}
        if self.path is not None:
            item["path"] = str(self.path)
        return item


def _hash(path: Path) -> str:
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise CycleDeploymentError("file-read-failed", str(exc), path) from exc
    return hashlib.sha256(value).hexdigest()


def _root(path: Path, label: str) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise CycleDeploymentError("relative-root", f"{label} must be absolute", path)
    if path.is_symlink():
        raise CycleDeploymentError("root-symlink", f"{label} may not be a symlink", path)
    unresolved = path
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise CycleDeploymentError("root-unavailable", f"{label}: {exc}", path) from exc
    if path != unresolved:
        raise CycleDeploymentError(
            "root-symlink",
            f"{label} contains a symlink or non-canonical component",
            unresolved,
        )
    if not path.is_dir():
        raise CycleDeploymentError("root-not-directory", f"{label} is not a directory", path)
    return path


def _file(root: Path, relative: str, *, required: bool) -> Path | None:
    path = root / relative
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if not required:
            return None
        raise CycleDeploymentError("file-missing", "required file is missing", path) from None
    except OSError as exc:
        raise CycleDeploymentError("file-unavailable", str(exc), path) from exc
    if stat.S_ISLNK(mode):
        raise CycleDeploymentError("file-symlink", "file may not be a symlink", path)
    if not stat.S_ISREG(mode):
        raise CycleDeploymentError("file-not-regular", "file is not regular", path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CycleDeploymentError("file-unavailable", str(exc), path) from exc
    if resolved != path:
        raise CycleDeploymentError("file-symlink", "file path contains a symlink", path)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CycleDeploymentError("path-escape", "file escapes its root", path) from exc
    return resolved


def _manifest(path: Path) -> dict[str, Any]:
    path = path.expanduser()
    if not path.is_absolute():
        raise CycleDeploymentError("relative-manifest", "manifest path must be absolute", path)
    if path.is_symlink():
        raise CycleDeploymentError("manifest-symlink", "manifest may not be a symlink", path)
    unresolved = path
    try:
        info = path.lstat()
    except OSError as exc:
        raise CycleDeploymentError("manifest-unavailable", str(exc), path) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
        raise CycleDeploymentError("manifest-invalid-file", "manifest file is invalid", path)
    try:
        resolved_manifest = path.resolve(strict=True)
    except OSError as exc:
        raise CycleDeploymentError("manifest-unavailable", str(exc), path) from exc
    if resolved_manifest != unresolved:
        raise CycleDeploymentError(
            "manifest-symlink",
            "manifest path contains a symlink or non-canonical component",
            unresolved,
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CycleDeploymentError("manifest-invalid-json", str(exc), path) from exc
    if not isinstance(value, dict):
        raise CycleDeploymentError("manifest-not-object", "manifest root must be an object", path)
    if value.get("schema_version") != 1 or value.get("kind") != "bureau_runtime_deployment":
        raise CycleDeploymentError("manifest-contract", "unsupported manifest contract", path)
    commit = value.get("source_commit")
    release = value.get("immutable_release_path")
    release_id = value.get("release_id")
    package_tree_sha256 = value.get("package_tree_sha256")
    if not isinstance(commit, str) or SHA40.fullmatch(commit) is None:
        raise CycleDeploymentError("manifest-commit", "source_commit is invalid", path)
    if not isinstance(release, str):
        raise CycleDeploymentError("manifest-release", "immutable_release_path is invalid", path)
    if not isinstance(release_id, str) or not release_id:
        raise CycleDeploymentError("manifest-release-id", "release_id is invalid", path)
    if not isinstance(package_tree_sha256, str) or SHA64.fullmatch(package_tree_sha256) is None:
        raise CycleDeploymentError("manifest-package-tree", "package_tree_sha256 is invalid", path)
    release_root = _root(Path(release), "immutable release")
    expected_release_id = f"{commit[:12]}-src{package_tree_sha256[:12]}"
    if release_id != expected_release_id or release_root.name != release_id:
        raise CycleDeploymentError(
            "manifest-release-identity",
            "release_id, source_commit, package_tree_sha256 and release path disagree",
            path,
        )
    result = dict(value)
    result["_release"] = release_root
    return result


def _finding(code: str, message: str, path: Path, stage: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": str(path), "stage": stage}


def _contract(path: Path, *, stage: str, kind: str, name: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CycleDeploymentError("canonical-contract-unreadable", str(exc), path) from exc
    if kind == "service":
        expected = f"ExecStart=%h/.local/bin/bureau cycle-run {stage}"
        lines = [line for line in text.splitlines() if line.startswith("ExecStart=")]
        if lines != [expected]:
            raise CycleDeploymentError(
                "canonical-unit-execstart",
                f"{stage} service must contain exactly {expected}",
                path,
            )
        for token in ("repos/bureau", ".local/libexec", "PYTHONPATH="):
            if token in text:
                raise CycleDeploymentError(
                    "canonical-unit-mutable-source",
                    f"{stage} service contains mutable source token {token}",
                    path,
                )
        required_address_families = (
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6"
            if stage == "closure"
            else "RestrictAddressFamilies=AF_UNIX"
        )
        required_hardening = {
            "Type=oneshot",
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            required_address_families,
            "LockPersonality=yes",
            "MemoryDenyWriteExecute=yes",
            "RestrictRealtime=yes",
            "SystemCallArchitectures=native",
        }
        missing = sorted(required_hardening.difference(text.splitlines()))
        if missing:
            raise CycleDeploymentError(
                "canonical-unit-hardening",
                f"{stage} service is missing hardening: {', '.join(missing)}",
                path,
            )
        return
    if kind == "timer":
        expected = f"Unit={name}.service"
        lines = [line for line in text.splitlines() if line.startswith("Unit=")]
        if lines != [expected]:
            raise CycleDeploymentError(
                "canonical-timer-target",
                f"{stage} timer must contain exactly {expected}",
                path,
            )
        return
    expected = f'exec "$HOME/.local/bin/bureau" cycle-run {stage} "$@"'
    if expected not in text.splitlines():
        raise CycleDeploymentError(
            "canonical-shim-exec",
            f"{stage} shim must contain {expected}",
            path,
        )
    for token in ("repos/bureau", "PYTHONPATH=", ".local/libexec/bureau_cycle"):
        if token in text:
            raise CycleDeploymentError(
                "canonical-shim-mutable-source",
                f"{stage} shim contains mutable source token {token}",
                path,
            )


def _compare(
    canonical: Path, live: Path | None, stage: str, kind: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    expected = _hash(canonical)
    live_hash = _hash(live) if live else None
    canonical_mode = stat.S_IMODE(canonical.stat().st_mode)
    live_mode = stat.S_IMODE(live.stat().st_mode) if live else None
    executable = kind != "shim" or bool(live_mode and live_mode & 0o111)
    observation = {
        "canonical_path": str(canonical),
        "canonical_sha256": expected,
        "canonical_mode": f"{canonical_mode:04o}",
        "live_path": str(live) if live else None,
        "live_sha256": live_hash,
        "live_mode": f"{live_mode:04o}" if live_mode is not None else None,
        "matches": bool(live and live_hash == expected and executable),
    }
    if live is None:
        return observation, [
            _finding(f"live-{kind}-missing", "live file is missing", canonical, stage)
        ]
    if kind == "shim" and not executable:
        return observation, [
            _finding("live-shim-not-executable", "live shim is not executable", live, stage)
        ]
    if observation["matches"]:
        return observation, []
    return observation, [_finding(f"live-{kind}-drift", "live file differs", live, stage)]


def _default_modules() -> dict[str, Path]:
    cycle = importlib.import_module("bureau_cycle")
    closure = importlib.import_module("bureau.closure_runner")
    return {
        "bureau.cycle_deployment": Path(__file__),
        "bureau_cycle": Path(str(cycle.__file__)),
        "bureau.closure_runner": Path(str(closure.__file__)),
    }


def audit_cycle_deployment(
    *,
    manifest_path: Path,
    unit_root: Path,
    shim_root: Path,
    canonical_root: Path | None = None,
    module_paths: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Return a deterministic audit. This function never writes or repairs."""

    manifest = _manifest(manifest_path)
    release = manifest["_release"]
    canonical = _root(canonical_root or release, "canonical root")
    units = _root(unit_root, "live unit root")
    shims = _root(shim_root, "live shim root")
    findings: list[dict[str, str]] = []
    expected_tree_sha256 = manifest["package_tree_sha256"]
    observed_tree_sha256 = _package_tree_sha256(release)
    release_identity = {
        "release_id": manifest["release_id"],
        "source_commit": manifest["source_commit"],
        "expected_package_tree_sha256": expected_tree_sha256,
        "observed_package_tree_sha256": observed_tree_sha256,
        "matches": observed_tree_sha256 == expected_tree_sha256,
    }
    if observed_tree_sha256 != expected_tree_sha256:
        findings.append(
            _finding(
                "runtime-release-package-tree-drift",
                "immutable release package tree differs from the deployment manifest",
                release,
                "runtime-release",
            )
        )
    sources = []
    for relative in SOURCES:
        path = _file(canonical, relative, required=True)
        assert path is not None
        sources.append({"path": relative, "sha256": _hash(path)})

    stages = []
    for stage, name, module in STAGES:
        service = _file(canonical, f"ops/systemd/{name}.service", required=True)
        timer = _file(canonical, f"ops/systemd/{name}.timer", required=True)
        shim = _file(canonical, f"ops/systemd/libexec/{name}", required=True)
        assert service and timer and shim
        if not stat.S_IMODE(shim.stat().st_mode) & 0o111:
            raise CycleDeploymentError(
                "canonical-shim-not-executable",
                "canonical compatibility shim is not executable",
                shim,
            )
        _contract(service, stage=stage, kind="service", name=name)
        _contract(timer, stage=stage, kind="timer", name=name)
        _contract(shim, stage=stage, kind="shim", name=name)
        service_obs, service_findings = _compare(
            service, _file(units, f"{name}.service", required=False), stage, "service"
        )
        timer_obs, timer_findings = _compare(
            timer, _file(units, f"{name}.timer", required=False), stage, "timer"
        )
        shim_obs, shim_findings = _compare(shim, _file(shims, name, required=False), stage, "shim")
        findings += service_findings + timer_findings + shim_findings
        stages.append(
            {
                "name": stage,
                "module": module,
                "service": service_obs,
                "timer": timer_obs,
                "compatibility_shim": shim_obs,
            }
        )

    modules = []
    for name, raw in sorted((module_paths or _default_modules()).items()):
        raw = raw.expanduser()
        if not raw.is_absolute():
            findings.append(
                _finding("runtime-module-relative", "module path is not absolute", raw, name)
            )
            continue
        if raw.is_symlink():
            findings.append(_finding("runtime-module-symlink", "module is a symlink", raw, name))
            continue
        try:
            path = raw.resolve(strict=True)
        except OSError as exc:
            findings.append(_finding("runtime-module-missing", str(exc), raw, name))
            continue
        if path != raw:
            findings.append(
                _finding(
                    "runtime-module-symlink",
                    "module path contains a symlink or non-canonical component",
                    raw,
                    name,
                )
            )
            continue
        inside = False
        try:
            path.relative_to(release)
            inside = True
        except ValueError:
            pass
        modules.append(
            {"name": name, "path": str(path), "sha256": _hash(path), "inside_release": inside}
        )
        if not inside:
            findings.append(
                _finding(
                    "runtime-module-outside-release",
                    "module is outside immutable release",
                    path,
                    name,
                )
            )

    findings.sort(key=lambda item: (item["code"], item.get("stage", ""), item.get("path", "")))
    public_manifest = {
        key: manifest.get(key)
        for key in (
            "schema_version",
            "kind",
            "release_id",
            "source_commit",
            "immutable_release_path",
            "package_tree_sha256",
        )
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "bureau_cycle_deployment_audit",
        "status": "ok" if not findings else "drift",
        "activatable": False,
        "read_only": True,
        "self_heal": False,
        "compatibility_name": "bureau-halfhour-operator",
        "manifest": public_manifest,
        "release_identity": release_identity,
        "canonical_root": str(canonical),
        "canonical_sources": sources,
        "runtime_modules": modules,
        "stages": stages,
        "findings": findings,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("~/.local/share/bureau/deployment-manifest.json").expanduser(),
    )
    parser.add_argument("--canonical-root", type=Path)
    parser.add_argument(
        "--unit-root", type=Path, default=Path("~/.config/systemd/user").expanduser()
    )
    parser.add_argument("--shim-root", type=Path, default=Path("~/.local/libexec").expanduser())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = audit_cycle_deployment(
            manifest_path=args.manifest,
            canonical_root=args.canonical_root,
            unit_root=args.unit_root,
            shim_root=args.shim_root,
        )
    except CycleDeploymentError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "kind": "bureau_cycle_deployment_audit",
            "status": "invalid",
            "activatable": False,
            "read_only": True,
            "self_heal": False,
            "findings": [exc.finding()],
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
