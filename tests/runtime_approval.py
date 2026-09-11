from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bureau import approval
from bureau import runtime_refresh as refresh

TASK_ID = "BUR-2026-003-T009"


def write_runtime_approval_intent(
    source: Path,
    directory: Path,
    *,
    label: str,
    expires_at: datetime | None = None,
) -> Path:
    source_head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    reference = hashlib.sha256(f"{source_head}:{label}".encode()).hexdigest()
    evidence = approval.break_glass_approval(
        source=f"test:{label}",
        approved=True,
        reviewer="pytest",
        reference=reference,
        task_id=TASK_ID,
        scope="runtime_mutation",
    )
    decision = approval.require_approval(
        "runtime_mutation",
        evidence,
        expected_reference=reference,
        task_id=TASK_ID,
    )
    now = datetime.now(timezone.utc)
    intent = refresh.bind_digest(
        {
            "schema_version": 1,
            "kind": "bureau_runtime_refresh_intent",
            "main_commit": source_head,
            "target_sha256": reference,
            "approval_task_id": TASK_ID,
            "runtime_approval": decision,
            "created_at": refresh.isoformat(now),
            "expires_at": refresh.isoformat(expires_at or now + timedelta(hours=1)),
        },
        "intent_sha256",
    )
    path = directory / f"runtime-approval-{label}.json"
    path.write_bytes(refresh.canonical_bytes(intent))
    return path


def run_runtime_installer_for_non_authority_test(
    command: list[str],
    *,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    """Run installer behavior tests without pretending pytest is a Grabowski executor.

    Packaging/launcher tests exercise the installer below its controller authority
    boundary. The production boundary itself is covered separately by the strict
    runtime-install authority tests. We still revalidate the immutable intent digest,
    expiry and exact source commit here; only live TaskSpec/executor/lease authority
    is replaced by this in-process test seam.
    """
    if len(command) < 2 or Path(command[1]).name != "install-bureau-runtime.py":
        raise ValueError("command must target install-bureau-runtime.py")
    installer_path = Path(command[1]).resolve()
    installer_digest = hashlib.sha256(str(installer_path).encode()).hexdigest()
    module_name = f"_bureau_installer_test_{installer_digest}"
    spec = importlib.util.spec_from_file_location(module_name, installer_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load installer module: {installer_path}")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)

    def validate_test_intent(
        intent_path: Path,
        *,
        expected_source_commit: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return refresh.validate_runtime_refresh_intent(
            intent_path,
            expected_source_commit=expected_source_commit,
            minimum_remaining_seconds=0,
        )

    original = refresh.validate_runtime_install_authority
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        refresh.validate_runtime_install_authority = validate_test_intent
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                result = installer.main(command[2:])
                returncode = 0 if result is None else int(result)
            except SystemExit as exc:
                returncode = exc.code if isinstance(exc.code, int) else 1
                if exc.code not in (None, 0) and not isinstance(exc.code, int):
                    print(str(exc.code), file=stderr)
    finally:
        refresh.validate_runtime_install_authority = original

    completed = subprocess.CompletedProcess(
        command,
        returncode,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )
    if check and returncode != 0:
        raise subprocess.CalledProcessError(
            returncode,
            command,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed
