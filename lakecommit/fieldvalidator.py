"""Reusable field-level validators for paths, partitions and files."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath

from lakecommit import config
from lakecommit.errors import SchemaError
from lakecommit.models import FileEntry, partition_label


def _mapping(value: object, location: str, field_path: str, code: str) -> dict:
    if not isinstance(value, dict):
        raise SchemaError(code, "expected an object", location=location, field_path=field_path)
    return value


def _string(value: object, location: str, field_path: str, code: str = "FIELD_REQUIRED") -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(code, "expected a non-empty string", location=location, field_path=field_path)
    return value.strip()


def _identifier(value: object, location: str, field_path: str, pattern, label: str) -> str:
    text = _string(value, location, field_path, "FIELD_FORMAT")
    if not pattern.match(text):
        raise SchemaError(
            "FIELD_FORMAT", f"invalid {label} format", location=location, field_path=field_path
        )
    return text


def logical_time(value: object, location: str, field_path: str) -> str:
    text = _string(value, location, field_path, "FIELD_FORMAT")
    if not config.LOGICAL_TIME_RE.match(text):
        raise SchemaError(
            "FIELD_FORMAT",
            "logical_time must use YYYY-MM-DDTHH:MM:SSZ",
            location=location,
            field_path=field_path,
        )
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SchemaError(
            "FIELD_FORMAT",
            "logical_time is not a real UTC instant",
            location=location,
            field_path=field_path,
        ) from exc
    return text


def validate_logical_file_path(value: object, location: str, field_path: str) -> str:
    text = _string(value, location, field_path, "PATH_INVALID")
    if "://" in text:
        raise SchemaError("PATH_URL", "file paths may not be URLs", location=location, field_path=field_path)
    if "\\" in text or ":" in text:
        raise SchemaError(
            "PATH_INVALID",
            "file paths must be POSIX-style logical paths",
            location=location,
            field_path=field_path,
        )
    path = PurePosixPath(text)
    if path.is_absolute() or text.startswith("/"):
        raise SchemaError("PATH_ABSOLUTE", "file paths must be relative", location=location, field_path=field_path)
    if ".." in path.parts:
        raise SchemaError(
            "PATH_TRAVERSAL",
            "file paths may not contain '..'",
            location=location,
            field_path=field_path,
        )
    if any(part in ("", ".") for part in text.split("/")):
        raise SchemaError(
            "PATH_INVALID", "file path contains an empty segment", location=location, field_path=field_path
        )
    if not all(character.isalnum() or character in "._=+-/" for character in text):
        raise SchemaError(
            "PATH_INVALID", "file path contains unsupported characters", location=location, field_path=field_path
        )
    return text


def validate_partition(
    value: object,
    partition_columns: tuple[str, ...],
    location: str,
    field_path: str,
) -> dict[str, str]:
    mapping = _mapping(value, location, field_path, "PARTITION_INVALID")
    expected = set(partition_columns)
    actual = set(mapping)
    if actual != expected:
        raise SchemaError(
            "PARTITION_MISMATCH",
            f"partition columns must be exactly {list(partition_columns)}",
            location=location,
            field_path=field_path,
            details={"expected": list(partition_columns), "actual": sorted(actual)},
        )
    result: dict[str, str] = {}
    for column in partition_columns:
        raw = mapping[column]
        if not isinstance(raw, str) or not config.PARTITION_VALUE_RE.match(raw):
            raise SchemaError(
                "PARTITION_VALUE_INVALID",
                "partition values must be stable non-empty identifiers",
                location=location,
                field_path=f"{field_path}.{column}",
            )
        result[column] = raw
    return {key: result[key] for key in sorted(result)}


def validate_file_document(
    value: object,
    partition_columns: tuple[str, ...],
    location: str,
    field_path: str,
) -> FileEntry:
    mapping = _mapping(value, location, field_path, "FILE_INVALID")
    path = validate_logical_file_path(mapping.get("path"), location, f"{field_path}.path")
    partition = validate_partition(
        mapping.get("partition"), partition_columns, location, f"{field_path}.partition"
    )
    for field in ("size_bytes", "row_count"):
        raw = mapping.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise SchemaError(
                "NON_NEGATIVE_INTEGER_REQUIRED",
                f"{field} must be a non-negative integer",
                location=location,
                field_path=f"{field_path}.{field}",
            )
    content_hash = _string(
        mapping.get("content_hash"), location, f"{field_path}.content_hash", "FILE_HASH_REQUIRED"
    )
    if not config.CONTENT_HASH_RE.match(content_hash):
        raise SchemaError(
            "FILE_HASH_INVALID",
            "content_hash has an unsupported format",
            location=location,
            field_path=f"{field_path}.content_hash",
        )
    return FileEntry(
        path=path,
        partition=partition,
        size_bytes=mapping["size_bytes"],
        row_count=mapping["row_count"],
        content_hash=content_hash,
    )


def _swap_group(value: object, columns: tuple[str, ...], location: str, field_path: str) -> dict:
    group = _mapping(value, location, field_path, "SWAP_GROUP_INVALID")
    partition = validate_partition(
        group.get("partition"), columns, location, f"{field_path}.partition"
    )
    expected_raw = group.get("expected_files")
    if not isinstance(expected_raw, list) or not expected_raw:
        raise SchemaError("EXPECTED_FILES_REQUIRED", "swap group expected_files must be a non-empty array",
                          location=location, field_path=f"{field_path}.expected_files")
    expected = [
        validate_logical_file_path(item, location, f"{field_path}.expected_files[{index}]")
        for index, item in enumerate(expected_raw)]
    if len(set(expected)) != len(expected):
        raise SchemaError("DUP_EXPECTED_FILE", "swap group expected_files may not repeat a path",
                          location=location, field_path=f"{field_path}.expected_files")
    files = validate_file_list(group.get("files"), columns, location, f"{field_path}.files")
    for index, entry in enumerate(files):
        if entry.partition != partition:
            raise SchemaError(
                "SWAP_GROUP_FILE_MISMATCH",
                "every replacement file must belong to its declared swap partition",
                location=location, field_path=f"{field_path}.files[{index}].partition")
    return {"partition": partition, "expected_files": sorted(expected),
            "files": [entry.to_dict() for entry in files]}


def validate_swap_operation(
    operation: dict, columns: tuple[str, ...], location: str, field_path: str,
) -> dict:
    swap_id = _identifier(
        operation.get("swap_id"), location, f"{field_path}.swap_id",
        config.OBJECT_ID_RE, "swap id",
    )
    authorization = operation.get("authorization")
    if authorization not in config.SWAP_AUTHORIZATION_ROUTES:
        raise SchemaError(
            "SWAP_AUTHORIZATION_INVALID",
            f"authorization must be one of {sorted(config.SWAP_AUTHORIZATION_ROUTES)}",
            location=location, field_path=f"{field_path}.authorization",
        )
    groups_raw = operation.get("groups")
    if not isinstance(groups_raw, list) or len(groups_raw) != 2:
        raise SchemaError(
            "SWAP_GROUPS_REQUIRED", "a swap must declare exactly two groups",
            location=location, field_path=f"{field_path}.groups",
        )
    groups = [
        _swap_group(item, columns, location, f"{field_path}.groups[{index}]")
        for index, item in enumerate(groups_raw)
    ]
    labels = [partition_label(group["partition"], columns) for group in groups]
    if labels[0] == labels[1]:
        raise SchemaError(
            "SWAP_PARTITION_MISMATCH", "the two swap groups must target different partitions",
            location=location, field_path=f"{field_path}.groups", details={"partitions": labels},
        )
    expected_sets = [set(group["expected_files"]) for group in groups]
    shared = sorted(expected_sets[0] & expected_sets[1])
    if shared:
        raise SchemaError("DUP_EXPECTED_FILE", "expected file sets must be disjoint across swap groups",
                          location=location, field_path=f"{field_path}.groups", details={"paths": shared})
    replacement_paths = [entry["path"] for group in groups for entry in group["files"]]
    if len(set(replacement_paths)) != len(replacement_paths):
        duplicates = sorted(
            {path for path in replacement_paths if replacement_paths.count(path) > 1})
        raise SchemaError("DUP_FILE_PATH", "replacement files may not repeat a path across swap groups",
                          location=location, field_path=f"{field_path}.groups", details={"paths": duplicates})
    overlaps = sorted(set(replacement_paths) & (expected_sets[0] | expected_sets[1]))
    if overlaps:
        raise SchemaError("SWAP_PATH_OVERLAP", "replacement paths may not overlap any expected file",
                          location=location, field_path=f"{field_path}.groups", details={"paths": overlaps})
    ordered = sorted(groups, key=lambda group: partition_label(group["partition"], columns))
    return {"type": "swap_file_sets", "swap_id": swap_id,
            "authorization": authorization, "groups": ordered}


def validate_file_list(value: object, columns, location, field_path) -> list[FileEntry]:
    if not isinstance(value, list) or not value:
        raise SchemaError(
            "FILES_REQUIRED", "files must be a non-empty array", location=location, field_path=field_path
        )
    files = [
        validate_file_document(item, columns, location, f"{field_path}[{index}]")
        for index, item in enumerate(value)]
    paths = [item.path for item in files]
    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    if duplicates:
        raise SchemaError("DUP_FILE_PATH", "files may not repeat a path",
                          location=location, field_path=field_path, details={"paths": duplicates})
    return sorted(files, key=lambda item: item.path)
