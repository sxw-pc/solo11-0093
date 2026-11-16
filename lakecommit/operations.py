"""Pure file-level operation evaluation with explicit rebase evidence."""

from __future__ import annotations

from dataclasses import dataclass, field

from lakecommit import conflicts
from lakecommit.conflicts import Code, reason
from lakecommit.models import Attempt, FileEntry, Snapshot, partition_label


@dataclass
class EvalResult:
    accepted: bool
    files: list[FileEntry]
    reasons: list[dict] = field(default_factory=list)
    conflict_codes: list[str] = field(default_factory=list)
    rebase: dict | None = None
    touched_partitions: list[str] = field(default_factory=list)
    added_paths: list[str] = field(default_factory=list)
    removed_paths: list[str] = field(default_factory=list)


def _reject(
    reasons: list[dict],
    codes: list[str],
    *,
    rebase: dict | None = None,
) -> EvalResult:
    return EvalResult(
        accepted=False,
        files=[],
        reasons=reasons,
        conflict_codes=list(dict.fromkeys(codes)),
        rebase=rebase,
    )


def _rebase_evidence(base: Snapshot, current: Snapshot, changes: list[dict],
                     strategy: str, checked: list[str]) -> dict:
    return {
        "base_snapshot_id": base.snapshot_id,
        "current_snapshot_id": current.snapshot_id,
        "intervening_snapshot_ids": [str(commit["snapshot_id"]) for commit in changes],
        "intervening_attempt_ids": [
            str(commit["attempt_id"]) for commit in changes if commit.get("attempt_id")],
        "strategy": strategy,
        "checked_partitions": sorted(set(checked)),
    }


def _accepted(current: Snapshot, files: list[FileEntry], attempt: Attempt, base: Snapshot,
              changes: list[dict], columns: tuple[str, ...], strategy: str,
              checked: list[str], allow_safe_rebase: bool) -> EvalResult:
    stale = base.snapshot_id != current.snapshot_id
    if stale and not allow_safe_rebase:
        detail = {"base_snapshot_id": base.snapshot_id, "current_snapshot_id": current.snapshot_id}
        return _reject(
            [reason(Code.STALE_BASE_REBASE_DISABLED, "safe rebase is disabled by the scenario protocol", details=detail)],
            [Code.STALE_BASE_REBASE_DISABLED])
    rebase = _rebase_evidence(base, current, changes, strategy, checked) if stale else None
    if rebase:
        detail = {"base_snapshot_id": base.snapshot_id, "current_snapshot_id": current.snapshot_id, "strategy": strategy}
        reasons = [reason(conflicts.INFO_SAFE_REBASE, "stale base accepted through a safe file-level rebase", severity="info", details=detail)]
    else:
        reasons = [reason(conflicts.INFO_CURRENT_BASE, "attempt was evaluated against the current snapshot", severity="info", details={"snapshot_id": current.snapshot_id})]
    ordered = sorted(files, key=lambda entry: entry.path)
    current_paths = current.paths()
    result_paths = {entry.path for entry in ordered}
    touched = {partition_label(entry.partition, columns) for entry in ordered if entry.path not in current_paths}
    touched.update(partition_label(entry.partition, columns) for entry in current.files if entry.path not in result_paths)
    return EvalResult(
        accepted=True, files=ordered, reasons=reasons, rebase=rebase,
        touched_partitions=sorted(touched),
        added_paths=sorted(result_paths - current_paths),
        removed_paths=sorted(current_paths - result_paths))


def _partition_touched(changes: list[dict]) -> set[str]:
    touched: set[str] = set()
    for commit in changes:
        touched.update(str(item) for item in commit.get("touched_partitions", []))
    return touched


def _duplicate_reason(paths: list[str], message: str) -> dict:
    return reason(
        Code.FILE_PATH_DUPLICATE,
        message,
        details={"paths": sorted(set(paths))},
    )


def evaluate_add(attempt: Attempt, base: Snapshot, current: Snapshot, changes: list[dict], columns: tuple[str, ...], allow_safe_rebase: bool) -> EvalResult:
    files = [FileEntry.from_dict(item) for item in attempt.operation["files"]]
    codes: list[str] = []
    reasons: list[dict] = []
    duplicate_paths = sorted(current.paths() & {entry.path for entry in files})
    if duplicate_paths:
        codes.append(Code.FILE_PATH_DUPLICATE)
        reasons.append(_duplicate_reason(duplicate_paths, "added file paths already exist in the current snapshot"))
    partitions = [partition_label(entry.partition, columns) for entry in files]
    touched = _partition_touched(changes) if base.snapshot_id != current.snapshot_id else set()
    blocked_partitions = sorted(set(partitions) & touched)
    if blocked_partitions:
        codes.append(Code.PARTITION_TOUCHED_SINCE_BASE)
        reasons.append(
            reason(
                Code.PARTITION_TOUCHED_SINCE_BASE,
                "one or more target partitions were touched after the base snapshot",
                details={"partitions": blocked_partitions},
            )
        )
    if codes:
        return _reject(reasons, codes)
    return _accepted(
        current, list(current.files) + files, attempt, base, changes, columns,
        "ADDITIVE_REBASE", partitions, allow_safe_rebase)


def evaluate_replace_partition(attempt: Attempt, base: Snapshot, current: Snapshot, changes: list[dict], columns: tuple[str, ...], allow_safe_rebase: bool) -> EvalResult:
    files = [FileEntry.from_dict(item) for item in attempt.operation["files"]]
    target = {key: str(attempt.operation["partition"][key]) for key in sorted(attempt.operation["partition"])}
    target_label = partition_label(target, columns)
    outside = [entry for entry in current.files if entry.partition != target]
    collisions = sorted({entry.path for entry in outside} & {entry.path for entry in files})
    codes: list[str] = []
    reasons: list[dict] = []
    if collisions:
        codes.append(Code.FILE_PATH_DUPLICATE)
        reasons.append(_duplicate_reason(collisions, "replacement file paths collide with files outside the target partition"))
    stale = base.snapshot_id != current.snapshot_id
    if stale and target_label in _partition_touched(changes):
        codes.extend([Code.REPLACE_PARTITION_CONFLICT, Code.PARTITION_TOUCHED_SINCE_BASE])
        reasons.append(reason(
            Code.REPLACE_PARTITION_CONFLICT,
            "the target partition changed after the base snapshot",
            details={"partition": target_label, "base_snapshot_id": base.snapshot_id, "current_snapshot_id": current.snapshot_id}))
        reasons.append(reason(
            Code.PARTITION_TOUCHED_SINCE_BASE,
            "partition replacement requires a current base snapshot",
            details={"partitions": [target_label]}))
    if codes:
        return _reject(reasons, codes)
    return _accepted(
        current, outside + files, attempt, base, changes, columns,
        "PARTITION_REPLACE_REBASE", [target_label], allow_safe_rebase)


def evaluate_replace_file_set(attempt: Attempt, base: Snapshot, current: Snapshot, changes: list[dict], columns: tuple[str, ...], allow_safe_rebase: bool) -> EvalResult:
    expected = sorted(str(item) for item in attempt.operation["expected_files"])
    files = [FileEntry.from_dict(item) for item in attempt.operation["files"]]
    base_map = base.files_by_path()
    current_map = current.files_by_path()
    missing_base = [path for path in expected if path not in base_map]
    changed = [
        path
        for path in expected
        if path in base_map
        and (path not in current_map or current_map[path].fingerprint() != base_map[path].fingerprint())
    ]
    codes: list[str] = []
    reasons: list[dict] = []
    if missing_base:
        codes.append(Code.FILE_SET_CHANGED_SINCE_BASE)
        reasons.append(reason(
            Code.FILE_SET_CHANGED_SINCE_BASE,
            "expected file set is not present in the base snapshot",
            details={"paths": missing_base}))
    if changed:
        codes.append(Code.FILE_SET_CHANGED_SINCE_BASE)
        reasons.append(reason(
            Code.FILE_SET_CHANGED_SINCE_BASE,
            "one or more expected files changed or disappeared after the base snapshot",
            details={"paths": sorted(changed)}))
    remaining = {entry.path for entry in current.files if entry.path not in set(expected)}
    collisions = sorted(remaining & {entry.path for entry in files})
    if collisions:
        codes.append(Code.FILE_PATH_DUPLICATE)
        reasons.append(_duplicate_reason(collisions, "replacement file paths collide with files outside the expected set"))
    if codes:
        return _reject(reasons, codes)
    result = [entry for entry in current.files if entry.path not in set(expected)] + files
    touched = [
        partition_label(entry.partition, columns)
        for entry in current.files if entry.path in set(expected)
    ] + [partition_label(entry.partition, columns) for entry in files]
    return _accepted(
        current, result, attempt, base, changes, columns,
        "FILE_SET_REPLACE_REBASE", touched, allow_safe_rebase)


def evaluate_operation(attempt: Attempt, base: Snapshot, current: Snapshot, changes: list[dict], columns: tuple[str, ...], allow_safe_rebase: bool) -> EvalResult:
    if attempt.operation_type == "add_files":
        return evaluate_add(attempt, base, current, changes, columns, allow_safe_rebase)
    if attempt.operation_type == "replace_partition":
        return evaluate_replace_partition(attempt, base, current, changes, columns, allow_safe_rebase)
    if attempt.operation_type == "swap_file_sets":
        from lakecommit.swap import evaluate_swap

        return evaluate_swap(attempt, base, current, changes, columns, allow_safe_rebase)
    return evaluate_replace_file_set(attempt, base, current, changes, columns, allow_safe_rebase)
