"""Schema and semantic validation for all local fixture inputs."""

from __future__ import annotations

from lakecommit import config
from lakecommit.errors import SchemaError
from lakecommit.fieldvalidator import (
    _identifier,
    _mapping,
    _string,
    logical_time as _logical_time,
    validate_file_document,
    validate_file_list,
    validate_logical_file_path,
    validate_partition,
    validate_swap_operation,
)
from lakecommit.fingerprint import object_fingerprint
from lakecommit.models import Attempt, FileEntry, Snapshot, TableSpec


def validate_table_document(value: object, location: str = "table.json") -> tuple[TableSpec, Snapshot]:
    table = _mapping(value, location, "$", "TABLE_INVALID")
    name = _identifier(table.get("table_name"), location, "table_name", config.TABLE_NAME_RE, "table name")
    columns_raw = table.get("partition_columns")
    if not isinstance(columns_raw, list) or not columns_raw:
        raise SchemaError("FIELD_REQUIRED", "partition_columns must be a non-empty array", location=location, field_path="partition_columns")
    columns = [
        _identifier(item, location, f"partition_columns[{index}]", config.COLUMN_RE, "column")
        for index, item in enumerate(columns_raw)
    ]
    if len(set(columns)) != len(columns):
        raise SchemaError("DUP_PARTITION_COLUMN", "partition columns must be unique", location=location, field_path="partition_columns")
    fields_raw = table.get("file_fields")
    if not isinstance(fields_raw, list) or tuple(fields_raw) != config.FILE_FIELDS:
        raise SchemaError(
            "FILE_FIELDS_INVALID",
            f"file_fields must be {list(config.FILE_FIELDS)}",
            location=location,
            field_path="file_fields",
        )
    spec = TableSpec(
        table_name=name,
        partition_columns=tuple(columns),
        file_fields=tuple(fields_raw),
        description=str(table.get("description", "")),
    )
    initial_raw = _mapping(table.get("initial_snapshot"), location, "initial_snapshot", "SNAPSHOT_INVALID")
    snapshot_id = _identifier(
        initial_raw.get("snapshot_id"),
        location,
        "initial_snapshot.snapshot_id",
        config.OBJECT_ID_RE,
        "snapshot id",
    )
    if initial_raw.get("parent_snapshot_id") is not None:
        raise SchemaError("INITIAL_SNAPSHOT_PARENT", "initial snapshot may not have a parent", location=location, field_path="initial_snapshot.parent_snapshot_id")
    sequence = initial_raw.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != 0:
        raise SchemaError("INITIAL_SNAPSHOT_SEQUENCE", "initial snapshot sequence must be 0", location=location, field_path="initial_snapshot.sequence")
    logical_time = _logical_time(initial_raw.get("logical_time"), location, "initial_snapshot.logical_time")
    files_raw = initial_raw.get("files")
    if not isinstance(files_raw, list):
        raise SchemaError("SNAPSHOT_FILES_INVALID", "initial files must be an array", location=location, field_path="initial_snapshot.files")
    files = [
        validate_file_document(item, tuple(columns), location, f"initial_snapshot.files[{index}]")
        for index, item in enumerate(files_raw)
    ]
    files.sort(key=lambda item: item.path)
    paths = [item.path for item in files]
    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    if duplicates:
        raise SchemaError("DUP_FILE_PATH", "initial snapshot contains duplicate paths", location=location, field_path="initial_snapshot.files", details={"paths": duplicates})
    snapshot = Snapshot(
        snapshot_id,
        None,
        0,
        logical_time,
        tuple(files),
        object_fingerprint([item.to_dict() for item in files]),
    )
    return spec, snapshot


def validate_operation(value: object, columns: tuple[str, ...], location: str, field_path: str) -> dict:
    operation = _mapping(value, location, field_path, "OPERATION_INVALID")
    operation_type = operation.get("type")
    if operation_type not in config.SUPPORTED_OPERATIONS:
        raise SchemaError(
            "UNSUPPORTED_OPERATION",
            f"operation type must be one of {list(config.SUPPORTED_OPERATIONS)}",
            location=location,
            field_path=f"{field_path}.type",
        )
    if operation_type == "swap_file_sets":
        return validate_swap_operation(operation, columns, location, field_path)
    files = validate_file_list(operation.get("files"), columns, location, f"{field_path}.files")
    if operation_type == "add_files":
        return {"type": operation_type, "files": [item.to_dict() for item in files]}
    if operation_type == "replace_partition":
        partition = validate_partition(
            operation.get("partition"), columns, location, f"{field_path}.partition"
        )
        for index, entry in enumerate(files):
            if entry.partition != partition:
                raise SchemaError(
                    "REPLACE_PARTITION_FILE_MISMATCH",
                    "every replacement file must belong to the target partition",
                    location=location,
                    field_path=f"{field_path}.files[{index}].partition",
                )
        return {
            "type": operation_type,
            "partition": partition,
            "files": [item.to_dict() for item in files],
        }
    expected_raw = operation.get("expected_files")
    if not isinstance(expected_raw, list) or not expected_raw:
        raise SchemaError("EXPECTED_FILES_REQUIRED", "expected_files must be a non-empty array", location=location, field_path=f"{field_path}.expected_files")
    expected = [
        validate_logical_file_path(item, location, f"{field_path}.expected_files[{index}]")
        for index, item in enumerate(expected_raw)
    ]
    if len(set(expected)) != len(expected):
        raise SchemaError("DUP_EXPECTED_FILE", "expected_files may not repeat a path", location=location, field_path=f"{field_path}.expected_files")
    return {
        "type": operation_type,
        "expected_files": sorted(expected),
        "files": [item.to_dict() for item in files],
    }


def validate_attempt_document(
    value: object,
    location: str,
    columns: tuple[str, ...],
    field_path: str = "$",
) -> Attempt:
    attempt = _mapping(value, location, field_path, "ATTEMPT_INVALID")
    required = {
        "attempt_id": config.OBJECT_ID_RE,
        "writer_id": config.OBJECT_ID_RE,
        "idempotency_key": config.OBJECT_ID_RE,
        "base_snapshot_id": config.OBJECT_ID_RE,
    }
    normalized: dict = {}
    for field, pattern in required.items():
        normalized[field] = _identifier(attempt.get(field), location, f"{field_path}.{field}", pattern, field)
    normalized["logical_time"] = _logical_time(
        attempt.get("logical_time"), location, f"{field_path}.logical_time"
    )
    normalized["operation"] = validate_operation(
        attempt.get("operation"), columns, location, f"{field_path}.operation"
    )
    retry_of = attempt.get("retry_of_attempt_id")
    if retry_of is not None:
        normalized["retry_of_attempt_id"] = _identifier(
            retry_of,
            location,
            f"{field_path}.retry_of_attempt_id",
            config.OBJECT_ID_RE,
            "retry attempt id",
        )
    return Attempt.from_dict(normalized)


def validate_attempts(
    documents: list[object],
    location: str,
    columns: tuple[str, ...],
    *,
    check_idempotency: bool = True,
) -> list[Attempt]:
    attempts = [
        validate_attempt_document(document, location, columns, field_path=f"attempts[{index}]")
        for index, document in enumerate(documents)
    ]
    ids = [attempt.attempt_id for attempt in attempts]
    duplicates = sorted({attempt_id for attempt_id in ids if ids.count(attempt_id) > 1})
    if duplicates:
        raise SchemaError(
            "DUP_ATTEMPT_ID",
            "attempt IDs must be unique",
            location=location,
            field_path="attempts",
            details={"attempt_ids": duplicates},
        )
    if check_idempotency:
        grouped: dict[tuple[str, str], str] = {}
        for index, attempt in enumerate(attempts):
            key = (attempt.writer_id, attempt.idempotency_key)
            digest = object_fingerprint(
                {"base_snapshot_id": attempt.base_snapshot_id, "operation": attempt.operation}
            )
            if key in grouped and grouped[key] != digest:
                raise SchemaError(
                    "IDEMPOTENCY_PAYLOAD_MISMATCH",
                    "the same writer/idempotency key has conflicting payloads",
                    location=location,
                    field_path=f"attempts[{index}].idempotency_key",
                )
            grouped.setdefault(key, digest)
    retry_ids = {attempt.attempt_id for attempt in attempts}
    for index, attempt in enumerate(attempts):
        if attempt.retry_of_attempt_id and attempt.retry_of_attempt_id not in retry_ids:
            raise SchemaError(
                "RETRY_ATTEMPT_UNKNOWN",
                "retry_of_attempt_id does not refer to this scenario",
                location=location,
                field_path=f"attempts[{index}].retry_of_attempt_id",
            )
    return attempts


def validate_scenario_document(value: object, location: str) -> dict:
    scenario = _mapping(value, location, "$", "SCENARIO_INVALID")
    scenario_id = _identifier(scenario.get("id"), location, "id", config.OBJECT_ID_RE, "scenario id")
    name = _string(scenario.get("name"), location, "name")
    description = str(scenario.get("description", ""))
    table_ref = _string(scenario.get("table"), location, "table")
    attempts_ref = _string(scenario.get("attempts"), location, "attempts")
    raw_protocol = scenario.get("protocol", config.default_protocol())
    protocol = _mapping(raw_protocol, location, "protocol", "PROTOCOL_INVALID")
    allow_safe_rebase = protocol.get("allow_safe_rebase", True)
    allow_idempotency_mismatch = protocol.get("allow_idempotency_mismatch", False)
    for field, value in (
        ("allow_safe_rebase", allow_safe_rebase),
        ("allow_idempotency_mismatch", allow_idempotency_mismatch),
    ):
        if not isinstance(value, bool):
            raise SchemaError(
                "FIELD_TYPE",
                f"{field} must be boolean",
                location=location,
                field_path=f"protocol.{field}",
            )
    return {
        "id": scenario_id,
        "name": name,
        "description": description,
        "table": table_ref,
        "attempts": attempts_ref,
        "protocol": {
            "allow_safe_rebase": allow_safe_rebase,
            "allow_idempotency_mismatch": allow_idempotency_mismatch,
        },
    }
