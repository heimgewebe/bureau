"""Automatic Bureau runtime convergence after a green ``main`` merge.

One run of ``bureau-runtime-refresh auto-update`` (driven by
``bureau-runtime-autoupdate.timer``) does exactly this:

1. observe GitHub ``main`` against the installed manifest (existing observer),
2. stop unless ``main`` is a green, proven descendant of the installed runtime,
3. clone ``main`` into a fresh detached workspace,
4. run the existing immutable installer, which converges the scheduler and
   restores its exact preimage itself if activation fails,
5. read the installed runtime back (``bureau --json check`` + runtime identity
   + scheduler); if that fails, restore the previous manifest, launchers and
   scheduler generation from the installer's own backup.

Enabling the timer is the standing authorization for this one update type.
Disabling it (``systemctl --user disable --now bureau-runtime-autoupdate.timer``)
turns automatic rollouts off. No per-merge TaskSpec is needed.

Outcomes: ``unchanged``, ``waiting``, ``busy``, ``activated``, ``rolled_back``,
``failed``. Only ``rolled_back``/``failed``/``blocked`` exit non-zero so the
systemd unit turns red where it needs attention.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import stat
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from . import runtime_refresh as rr

WAITING_REASONS = frozenset(
    {
        "required-ci-not-green",
        "main-changed-during-observation",
        "merged-main-pr-ambiguous",
    }
)
EXIT_CODES = {
    "unchanged": 0,
    "waiting": 0,
    "busy": 0,
    "activated": 0,
    "blocked": 3,
    "rolled_back": 4,
    "failed": 5,
}
MAX_HISTORY = 200


def exit_code(result: dict[str, Any]) -> int:
    return EXIT_CODES.get(str(result.get("status")), 5)


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _live_runtime_leases(
    resource_db: Path, *, prefix: Path, bin_dir: Path, now: datetime
) -> list[dict[str, Any]]:
    """Return live Grabowski leases on the runtime paths (manual refresh in flight)."""
    if not resource_db.is_file():
        return []
    keys = [
        f"path:{prefix}",
        *(f"path:{bin_dir / name}" for name, _ in rr.RUNTIME_LAUNCHER_ENTRYPOINTS),
    ]
    return rr._live_leases_for_resource_keys(resource_db, keys, now=now)


def rollback_to_previous(
    receipt: dict[str, Any],
    *,
    manifest_path: Path,
    command_runner: Callable[[list[str]], Any] | None = None,
) -> list[dict[str, Any]]:
    """Restore the installer backup generation recorded in ``receipt``.

    Returns a list of failures; empty means the previous runtime is back.
    """
    failures: list[dict[str, Any]] = []
    backup = receipt.get("rollback")
    if not isinstance(backup, dict) or not isinstance(backup.get("directory"), str):
        return [{"operation": "rollback", "error": "install receipt has no backup generation"}]

    def restore(target: Path, source: str | None) -> None:
        if not source:
            return
        backup_file = Path(source)
        if backup_file.is_symlink() or not backup_file.is_file():
            raise OSError(f"backup unavailable: {backup_file}")
        data = backup_file.read_bytes()
        mode = stat.S_IMODE(backup_file.stat().st_mode)
        if target.is_file() and not target.is_symlink() and target.read_bytes() == data:
            return
        rr.atomic_write(target, data, mode)

    try:
        restore(manifest_path, backup.get("manifest"))
    except Exception as exc:  # every recovery failure stays explicit
        failures.append({"operation": "restore-manifest", "error": repr(exc)})
    for receipt_field, backup_field in (
        ("launcher_path", "launcher"),
        ("runtime_refresh_launcher_path", "runtime_refresh_launcher"),
        ("status_capsule_launcher_path", "status_capsule_launcher"),
    ):
        target = receipt.get(receipt_field)
        if not isinstance(target, str):
            continue
        if backup.get(f"{backup_field}_kind") not in {"file", None}:
            failures.append(
                {"operation": f"restore-{backup_field}", "error": "unsupported backup kind"}
            )
            continue
        try:
            restore(Path(target), backup.get(backup_field))
        except Exception as exc:
            failures.append({"operation": f"restore-{backup_field}", "error": repr(exc)})

    preimage_path = Path(backup["directory"]) / "scheduler" / "preimage.json"
    if preimage_path.is_file():
        try:
            preimage = rr.read_json(preimage_path)
            failures.extend(
                rr._restore_scheduler_preimage(
                    preimage,
                    mutated_enablement_paths=set(),
                    command_runner=command_runner or (lambda argv: rr._run(argv, timeout=60)),
                )
            )
        except Exception as exc:
            failures.append({"operation": "restore-scheduler", "error": repr(exc)})
    return failures


def _record(state_root: Path, result: dict[str, Any]) -> dict[str, Any]:
    root = state_root / "auto-update"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = rr.canonical_bytes(result)
    rr.atomic_write(root / "last-result.json", data)
    if result.get("status") not in {"unchanged", "waiting", "busy"}:
        history = root / "history"
        history.mkdir(exist_ok=True, mode=0o700)
        stamp = str(result["finished_at"]).replace(":", "").replace("-", "")
        rr.atomic_write(history / f"{stamp}-{result['status']}.json", data)
        entries = sorted(history.glob("*.json"))
        for stale in entries[:-MAX_HISTORY]:
            with contextlib.suppress(OSError):
                stale.unlink()
    return result


def run_auto_update(
    *,
    state_root: Path,
    manifest_path: Path,
    repository: str = rr.DEFAULT_REPOSITORY,
    remote_url: str = f"https://github.com/{rr.DEFAULT_REPOSITORY}.git",
    prefix: Path,
    bin_dir: Path,
    user_unit_dir: Path,
    libexec_dir: Path,
    runtime_user_unit_dir: Path,
    resource_db: Path = rr.DEFAULT_GRABOWSKI_RESOURCE_DB,
    now: Callable[[], datetime] = rr.utc_now,
    observer: Callable[..., dict[str, Any]] = rr.observe_runtime_refresh,
    source_preparer: Callable[..., dict[str, Any]] = rr.prepare_source_checkout,
    installer: Callable[..., dict[str, Any]] = rr.run_installer,
    readback: Callable[..., dict[str, Any]] = rr.readback_install,
    rollback: Callable[..., list[dict[str, Any]]] = rollback_to_previous,
) -> dict[str, Any]:
    started_at = now()
    base: dict[str, Any] = {
        "schema_version": 1,
        "kind": "bureau_runtime_auto_update_result",
        "started_at": rr.isoformat(started_at),
    }

    def finish(status: str, **fields: Any) -> dict[str, Any]:
        return _record(
            state_root,
            {**base, **fields, "status": status, "finished_at": rr.isoformat(now())},
        )

    with _exclusive_lock(state_root / "auto-update" / "lock") as acquired:
        if not acquired:
            return {**base, "status": "busy", "reason": "another auto-update is running"}
        try:
            leases = _live_runtime_leases(
                resource_db, prefix=prefix, bin_dir=bin_dir, now=started_at
            )
        except rr.RuntimeRefreshError as exc:
            leases = []
            base["lease_check_error"] = exc.as_dict()
        if leases:
            return finish("busy", reason="runtime paths are leased", leases=leases)

        try:
            observation = observer(repository=repository, manifest_path=manifest_path)
            observation_path = rr.persist_observation(state_root, observation)
        except rr.RuntimeRefreshError as exc:
            return finish("waiting", reason="observation-failed", error=exc.as_dict())
        base.update(
            {
                "main_commit": observation.get("main_commit"),
                "previous_source_commit": observation.get("deployed_source_commit"),
                "observation_path": str(observation_path),
            }
        )
        status = observation.get("status")
        reasons = list(observation.get("reason_codes") or [])
        if status == "already_current":
            return finish("unchanged")
        if status not in {"candidate", "alert"}:
            outcome = "waiting" if reasons and set(reasons) <= WAITING_REASONS else "blocked"
            return finish(outcome, reason_codes=reasons)

        main_commit = str(observation["main_commit"])
        workspaces_root = state_root / "auto-update" / "workspaces"
        stamp = rr.isoformat(started_at).replace(":", "").replace("-", "")
        workspace = workspaces_root / f"{stamp}-{main_commit[:12]}"
        try:
            source = source_preparer(
                remote_url=remote_url,
                workspace=workspace,
                expected_commit=main_commit,
                workspaces_root=workspaces_root,
            )
        except rr.RuntimeRefreshError as exc:
            shutil.rmtree(workspace, ignore_errors=True)
            return finish("waiting", reason="source-checkout-failed", error=exc.as_dict())

        try:
            receipt = installer(
                source=Path(source["root"]),
                prefix=prefix,
                bin_dir=bin_dir,
                user_unit_dir=user_unit_dir,
                libexec_dir=libexec_dir,
                runtime_user_unit_dir=runtime_user_unit_dir,
                standing_auto_update=observation_path,
            )
        except rr.RuntimeRefreshError as exc:
            shutil.rmtree(workspace, ignore_errors=True)
            if exc.code == "scheduler-convergence-rolled-back":
                return finish("rolled_back", stage="install", error=exc.as_dict())
            return finish("failed", stage="install", error=exc.as_dict())

        try:
            verified = readback(
                expected_commit=main_commit,
                prefix=prefix,
                bin_dir=bin_dir,
                install_receipt=receipt,
            )
        except Exception as exc:  # any readback failure triggers rollback
            error = (
                exc.as_dict()
                if isinstance(exc, rr.RuntimeRefreshError)
                else {"code": "readback-unexpected", "message": repr(exc)}
            )
            failures = rollback(receipt, manifest_path=manifest_path)
            shutil.rmtree(workspace, ignore_errors=True)
            if failures:
                return finish(
                    "failed",
                    stage="readback",
                    error=error,
                    rollback_failures=failures,
                    receipt_path=receipt.get("receipt_path"),
                )
            return finish(
                "rolled_back",
                stage="readback",
                error=error,
                receipt_path=receipt.get("receipt_path"),
            )

        shutil.rmtree(workspace, ignore_errors=True)
        return finish(
            "activated",
            release_id=verified.get("release_id"),
            manifest_sha256=verified.get("manifest_sha256"),
            receipt_path=receipt.get("receipt_path"),
            rollback_directory=(receipt.get("rollback") or {}).get("directory"),
            pull_request=observation.get("pull_request"),
        )


def main() -> int:  # pragma: no cover - thin alias for python -m
    return rr.main(["auto-update"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["exit_code", "rollback_to_previous", "run_auto_update"]
