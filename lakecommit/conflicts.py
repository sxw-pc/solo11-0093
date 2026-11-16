"""Stable conflict codes and explainable reason records."""

from __future__ import annotations


class Code:
    BASE_SNAPSHOT_UNKNOWN = "BASE_SNAPSHOT_UNKNOWN"
    BASE_SNAPSHOT_NOT_ANCESTOR = "BASE_SNAPSHOT_NOT_ANCESTOR"
    FILE_PATH_DUPLICATE = "FILE_PATH_DUPLICATE"
    PARTITION_TOUCHED_SINCE_BASE = "PARTITION_TOUCHED_SINCE_BASE"
    FILE_SET_CHANGED_SINCE_BASE = "FILE_SET_CHANGED_SINCE_BASE"
    REPLACE_PARTITION_CONFLICT = "REPLACE_PARTITION_CONFLICT"
    IDEMPOTENCY_PAYLOAD_MISMATCH = "IDEMPOTENCY_PAYLOAD_MISMATCH"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    STALE_BASE_REBASE_DISABLED = "STALE_BASE_REBASE_DISABLED"
    STATE_INTEGRITY_ERROR = "STATE_INTEGRITY_ERROR"
    SWAP_EXPECTED_PARTITION_MISMATCH = "SWAP_EXPECTED_PARTITION_MISMATCH"
    SWAP_PARTITION_TOUCHED_SINCE_BASE = "SWAP_PARTITION_TOUCHED_SINCE_BASE"
    SWAP_PATH_OVERLAP = "SWAP_PATH_OVERLAP"


CONFLICT_CODES = (
    Code.BASE_SNAPSHOT_UNKNOWN,
    Code.BASE_SNAPSHOT_NOT_ANCESTOR,
    Code.FILE_PATH_DUPLICATE,
    Code.PARTITION_TOUCHED_SINCE_BASE,
    Code.FILE_SET_CHANGED_SINCE_BASE,
    Code.REPLACE_PARTITION_CONFLICT,
    Code.IDEMPOTENCY_PAYLOAD_MISMATCH,
    Code.VALIDATION_ERROR,
    Code.STALE_BASE_REBASE_DISABLED,
    Code.STATE_INTEGRITY_ERROR,
    Code.SWAP_EXPECTED_PARTITION_MISMATCH,
    Code.SWAP_PARTITION_TOUCHED_SINCE_BASE,
    Code.SWAP_PATH_OVERLAP,
)

REBASE_REQUIRED_CODES = frozenset(
    {
        Code.BASE_SNAPSHOT_UNKNOWN,
        Code.BASE_SNAPSHOT_NOT_ANCESTOR,
        Code.PARTITION_TOUCHED_SINCE_BASE,
        Code.FILE_SET_CHANGED_SINCE_BASE,
        Code.REPLACE_PARTITION_CONFLICT,
        Code.STALE_BASE_REBASE_DISABLED,
        Code.SWAP_PARTITION_TOUCHED_SINCE_BASE,
    }
)

INFO_SAFE_REBASE = "SAFE_REBASE_ACCEPTED"
INFO_CURRENT_BASE = "ACCEPTED_ON_CURRENT_BASE"
INFO_IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"


def reason(
    code: str,
    message: str,
    *,
    severity: str = "error",
    details: dict | None = None,
) -> dict:
    """Build a deterministic reason record."""
    value: dict = {"code": code, "severity": severity, "message": message}
    if details:
        value["details"] = details
    return value


def unique_codes(codes: list[str]) -> list[str]:
    return list(dict.fromkeys(codes))


def needs_rebase(codes: list[str]) -> bool:
    return any(code in REBASE_REQUIRED_CODES for code in codes)


def conflict_counts(decisions: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        for code in decision.conflict_codes:
            counts[code] = counts.get(code, 0) + 1
    return {key: counts[key] for key in sorted(counts)}
