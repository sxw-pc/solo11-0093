"""Atomic two-partition file-set swap evaluation.

A swap is one attempt that replaces one expected file set in each of two
different partitions. Both sides are checked against the same base/current
pair and the whole attempt is rejected unless every side is satisfiable; the
protocol then publishes exactly one snapshot.
"""

from __future__ import annotations

from lakecommit.conflicts import Code, reason
from lakecommit.models import Attempt, FileEntry, Snapshot, partition_label
from lakecommit.operations import EvalResult, _accepted, _partition_touched, _reject


def evaluate_swap(
    attempt: Attempt, base: Snapshot, current: Snapshot, changes: list[dict],
    columns: tuple[str, ...], allow_safe_rebase: bool,
) -> EvalResult:
    operation = attempt.operation
    groups = operation["groups"]
    labels = [partition_label(group["partition"], columns) for group in groups]
    base_map, current_map = base.files_by_path(), current.files_by_path()
    codes: list[str] = []
    reasons: list[dict] = []
    all_expected: set[str] = set()
    replacements: list[FileEntry] = []
    for group, label in zip(groups, labels):
        expected = set(group["expected_files"])
        files = [FileEntry.from_dict(item) for item in group["files"]]
        all_expected |= expected
        replacements.extend(files)
        missing_base = sorted(path for path in expected if path not in base_map)
        if missing_base:
            codes.append(Code.FILE_SET_CHANGED_SINCE_BASE)
            reasons.append(reason(
                Code.FILE_SET_CHANGED_SINCE_BASE,
                "expected swap files are not present in the base snapshot",
                details={"partition": label, "paths": missing_base}))
        wrong = sorted(
            path for path in expected if path in base_map
            and partition_label(base_map[path].partition, columns) != label)
        if wrong:
            codes.append(Code.SWAP_EXPECTED_PARTITION_MISMATCH)
            reasons.append(reason(
                Code.SWAP_EXPECTED_PARTITION_MISMATCH,
                "an expected file belongs to a different partition than declared",
                details={"partition": label, "paths": wrong}))
        changed = sorted(
            path for path in expected if path in base_map and (
                path not in current_map
                or current_map[path].fingerprint() != base_map[path].fingerprint()))
        if changed:
            codes.append(Code.FILE_SET_CHANGED_SINCE_BASE)
            reasons.append(reason(
                Code.FILE_SET_CHANGED_SINCE_BASE,
                "expected swap files changed or disappeared after the base snapshot",
                details={"partition": label, "paths": changed}))
    stale = base.snapshot_id != current.snapshot_id
    if stale and operation["authorization"] == "strict_partition":
        blocked = sorted(set(labels) & _partition_touched(changes))
        if blocked:
            codes.append(Code.SWAP_PARTITION_TOUCHED_SINCE_BASE)
            reasons.append(reason(
                Code.SWAP_PARTITION_TOUCHED_SINCE_BASE,
                "strict_partition swap rejects any concurrent touch of a target partition",
                details={"partitions": blocked, "base_snapshot_id": base.snapshot_id,
                         "current_snapshot_id": current.snapshot_id}))
    remaining = {entry.path for entry in current.files if entry.path not in all_expected}
    collisions = sorted(remaining & {entry.path for entry in replacements})
    if collisions:
        codes.append(Code.FILE_PATH_DUPLICATE)
        reasons.append(reason(
            Code.FILE_PATH_DUPLICATE,
            "swap replacement paths collide with files outside the expected sets",
            details={"paths": collisions}))
    if codes:
        return _reject(reasons, codes)
    result = [entry for entry in current.files if entry.path not in all_expected] + replacements
    touched = labels + [partition_label(entry.partition, columns) for entry in replacements]
    return _accepted(
        current, result, attempt, base, changes, columns,
        "SWAP_PARTITION_REBASE", touched, allow_safe_rebase)
