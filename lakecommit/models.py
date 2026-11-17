"""Small immutable-ish domain models shared by protocol and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lakecommit.fingerprint import object_fingerprint


def partition_label(partition: dict[str, str], columns: tuple[str, ...] | list[str]) -> str:
    return "|".join(f"{column}={partition[column]}" for column in columns)


@dataclass(frozen=True)
class FileEntry:
    path: str
    partition: dict[str, str]
    size_bytes: int
    row_count: int
    content_hash: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "partition": {key: self.partition[key] for key in sorted(self.partition)},
            "size_bytes": self.size_bytes,
            "row_count": self.row_count,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "FileEntry":
        return cls(
            path=str(value["path"]),
            partition={str(k): str(v) for k, v in sorted(value["partition"].items())},
            size_bytes=int(value["size_bytes"]),
            row_count=int(value["row_count"]),
            content_hash=str(value["content_hash"]),
        )

    def fingerprint(self) -> str:
        return object_fingerprint(self.to_dict())


@dataclass(frozen=True)
class TableSpec:
    table_name: str
    partition_columns: tuple[str, ...]
    file_fields: tuple[str, ...]
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "table_name": self.table_name,
            "partition_columns": list(self.partition_columns),
            "file_fields": list(self.file_fields),
            "description": self.description,
        }


@dataclass
class Snapshot:
    snapshot_id: str
    parent_snapshot_id: str | None
    sequence: int
    logical_time: str
    files: tuple[FileEntry, ...]
    manifest_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "sequence": self.sequence,
            "logical_time": self.logical_time,
            "file_count": len(self.files),
            "manifest_hash": self.manifest_hash,
            "files": [entry.to_dict() for entry in self.files],
        }

    def files_by_path(self) -> dict[str, FileEntry]:
        return {entry.path: entry for entry in self.files}

    def paths(self) -> set[str]:
        return {entry.path for entry in self.files}


@dataclass
class Attempt:
    attempt_id: str
    writer_id: str
    idempotency_key: str
    base_snapshot_id: str
    logical_time: str
    operation: dict[str, Any]
    retry_of_attempt_id: str | None = None

    @property
    def operation_type(self) -> str:
        return str(self.operation["type"])

    def to_dict(self) -> dict:
        value = {
            "attempt_id": self.attempt_id,
            "writer_id": self.writer_id,
            "idempotency_key": self.idempotency_key,
            "base_snapshot_id": self.base_snapshot_id,
            "logical_time": self.logical_time,
            "operation": self.operation,
        }
        if self.retry_of_attempt_id:
            value["retry_of_attempt_id"] = self.retry_of_attempt_id
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "Attempt":
        return cls(
            attempt_id=str(value["attempt_id"]),
            writer_id=str(value["writer_id"]),
            idempotency_key=str(value["idempotency_key"]),
            base_snapshot_id=str(value["base_snapshot_id"]),
            logical_time=str(value["logical_time"]),
            operation=value["operation"],
            retry_of_attempt_id=value.get("retry_of_attempt_id"),
        )


@dataclass
class Decision:
    attempt: Attempt
    status: str
    outcome: str
    payload_hash: str
    reasons: list[dict] = field(default_factory=list)
    conflict_codes: list[str] = field(default_factory=list)
    retry_required: bool = False
    rebase: dict | None = None
    resulting_snapshot_id: str | None = None
    snapshot_created: bool = False
    replay: dict | None = None

    @classmethod
    def rejected(
        cls,
        attempt: Attempt,
        payload_hash: str,
        reasons: list[dict],
        conflict_codes: list[str],
        *,
        retry_required: bool = False,
        rebase: dict | None = None,
    ) -> "Decision":
        return cls(
            attempt=attempt,
            status="rejected",
            outcome="rejected",
            payload_hash=payload_hash,
            reasons=reasons,
            conflict_codes=list(dict.fromkeys(conflict_codes)),
            retry_required=retry_required,
            rebase=rebase,
        )

    @classmethod
    def accepted(
        cls,
        attempt: Attempt,
        payload_hash: str,
        reasons: list[dict],
        snapshot_id: str,
        *,
        rebase: dict | None = None,
    ) -> "Decision":
        return cls(
            attempt=attempt,
            status="accepted",
            outcome="applied",
            payload_hash=payload_hash,
            reasons=reasons,
            conflict_codes=[],
            rebase=rebase,
            resulting_snapshot_id=snapshot_id,
            snapshot_created=True,
        )

    def core_dict(self) -> dict:
        value = {
            "attempt_id": self.attempt.attempt_id,
            "writer_id": self.attempt.writer_id,
            "idempotency_key": self.attempt.idempotency_key,
            "base_snapshot_id": self.attempt.base_snapshot_id,
            "logical_time": self.attempt.logical_time,
            "operation": self.attempt.operation,
            "retry_of_attempt_id": self.attempt.retry_of_attempt_id,
            "operation_type": self.attempt.operation_type,
            "payload_hash": self.payload_hash,
            "status": self.status,
            "outcome": self.outcome,
            "conflict_codes": list(self.conflict_codes),
            "retry_required": self.retry_required,
            "reasons": self.reasons,
            "rebase": self.rebase,
            "resulting_snapshot_id": self.resulting_snapshot_id,
            "snapshot_created": self.snapshot_created,
            "replay": self.replay,
        }
        return value

    def to_dict(self) -> dict:
        return self.core_dict()


@dataclass
class ScenarioBundle:
    scenario_id: str
    name: str
    description: str
    scenario_path: str
    table: TableSpec
    initial_snapshot: Snapshot
    attempts: list[Attempt]
    protocol: dict

    def to_report_meta(self) -> dict:
        return {
            "id": self.scenario_id,
            "name": self.name,
            "description": self.description,
        }
