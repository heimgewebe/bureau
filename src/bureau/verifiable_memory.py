from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

STATUS_ORDER = (
    "changed",
    "missing",
    "stale",
    "unverifiable",
    "still_established",
)
NON_CLAIMS = [
    "memory_claim_truth",
    "source_truth",
    "repo_understood",
    "runtime_correctness",
]
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _evidence_key(item: dict[str, Any]) -> str:
    for field in ("evidence_id", "citation_id", "range_ref"):
        value = item.get(field)
        if isinstance(value, str) and value:
            return value
    path = item.get("source_path") or item.get("path")
    start = item.get("start_line")
    end = item.get("end_line")
    if isinstance(path, str) and isinstance(start, int) and isinstance(end, int):
        return f"{path}:{start}-{end}"
    return ""


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _freshness_failure(
    item: dict[str, Any], observation: dict[str, Any], now: datetime
) -> tuple[str, str] | None:
    max_age = observation.get("max_age_hours", item.get("max_age_hours"))
    if not isinstance(max_age, int) or max_age < 0:
        return "unverifiable", "max_age_hours_missing_or_invalid"
    generated_at = _parse_datetime(observation.get("generated_at") or item.get("generated_at"))
    if generated_at is None:
        return "stale", "freshness_timestamp_missing_or_invalid"
    age_hours = (now - generated_at).total_seconds() / 3600
    if age_hours > max_age:
        return "stale", "freshness_expired"
    return None


def evaluate_memory_recall(
    memory_record: dict[str, Any],
    observations: dict[str, dict[str, Any]],
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate whether an evidence-bound memory may be used as current context.

    The function deliberately does not decide whether the remembered claim is true.
    It only checks whether the cited evidence is still present, hash-consistent and
    fresh enough for recall.
    """

    now = (checked_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not isinstance(observations, dict):
        observations = {}

    evidence_items = memory_record.get("evidence")
    if not isinstance(evidence_items, list):
        evidence_items = []

    checked: list[dict[str, Any]] = []
    counts = {status: 0 for status in STATUS_ORDER}

    for item in evidence_items:
        if not isinstance(item, dict):
            checked.append({"status": "unverifiable", "reason": "evidence_item_not_object"})
            counts["unverifiable"] += 1
            continue
        key = _evidence_key(item)
        observation = observations.get(key) if key else None
        if not isinstance(observation, dict):
            checked.append(
                {
                    "evidence_id": key or None,
                    "status": "missing",
                    "reason": "observation_missing",
                }
            )
            counts["missing"] += 1
            continue

        if observation.get("status") == "missing":
            status = "missing"
            reason = "source_missing"
        else:
            expected_hash = item.get("expected_sha256")
            observed_hash = observation.get("observed_sha256")
            if not _valid_sha256(expected_hash):
                status = "unverifiable"
                reason = "expected_hash_missing_or_invalid"
            elif not _valid_sha256(observed_hash):
                status = "unverifiable"
                reason = "observed_hash_missing_or_invalid"
            elif expected_hash != observed_hash:
                status = "changed"
                reason = "hash_mismatch"
            elif freshness_failure := _freshness_failure(item, observation, now):
                status, reason = freshness_failure
            else:
                status = "still_established"
                reason = "hash_and_freshness_ok"

        counts[status] += 1
        checked.append(
            {
                "evidence_id": key or None,
                "status": status,
                "reason": reason,
                "kind": item.get("kind"),
            }
        )

    if not checked:
        overall_status = "unverifiable"
    else:
        overall_status = next(status for status in STATUS_ORDER if counts[status])

    usable_for_context = overall_status == "still_established"
    return {
        "schema_version": 1,
        "checked_at": now.isoformat().replace("+00:00", "Z"),
        "memory_id": memory_record.get("memory_id"),
        "overall_status": overall_status,
        "usable_for_context": usable_for_context,
        "presentable_as_source_truth": False,
        "counts": counts,
        "evidence": checked,
        "does_not_establish": NON_CLAIMS,
    }
