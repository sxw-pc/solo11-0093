from types import SimpleNamespace

from lakecommit import conflicts
from lakecommit.conflicts import Code, reason


def test_all_required_conflict_codes_are_stable():
    required = {
        "BASE_SNAPSHOT_UNKNOWN",
        "FILE_PATH_DUPLICATE",
        "PARTITION_TOUCHED_SINCE_BASE",
        "FILE_SET_CHANGED_SINCE_BASE",
        "REPLACE_PARTITION_CONFLICT",
        "IDEMPOTENCY_PAYLOAD_MISMATCH",
        "VALIDATION_ERROR",
    }
    assert required <= set(conflicts.CONFLICT_CODES)
    assert len(set(conflicts.CONFLICT_CODES)) == len(conflicts.CONFLICT_CODES)


def test_reason_has_stable_shape_and_optional_details():
    value = reason(Code.FILE_PATH_DUPLICATE, "duplicate", details={"paths": ["b", "a"]})
    assert value == {
        "code": "FILE_PATH_DUPLICATE",
        "severity": "error",
        "message": "duplicate",
        "details": {"paths": ["b", "a"]},
    }
    assert "details" not in reason("X", "plain")


def test_unique_codes_preserve_first_occurrence_order():
    assert conflicts.unique_codes(["B", "A", "B", "C", "A"]) == ["B", "A", "C"]


def test_rebase_requirement_and_conflict_counts():
    decisions = [
        SimpleNamespace(conflict_codes=["BASE_SNAPSHOT_UNKNOWN", "FILE_PATH_DUPLICATE"]),
        SimpleNamespace(conflict_codes=["FILE_PATH_DUPLICATE"]),
        SimpleNamespace(conflict_codes=[]),
    ]
    assert conflicts.needs_rebase(["PARTITION_TOUCHED_SINCE_BASE"]) is True
    assert conflicts.needs_rebase(["FILE_PATH_DUPLICATE"]) is False
    assert conflicts.conflict_counts(decisions) == {
        "BASE_SNAPSHOT_UNKNOWN": 1,
        "FILE_PATH_DUPLICATE": 2,
    }
