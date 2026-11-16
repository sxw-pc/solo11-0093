"""Persistent publish-intent and recovery-resolution journal."""

from __future__ import annotations

import json
from pathlib import Path

from lakecommit import config
from lakecommit.errors import StateError
from lakecommit.fingerprint import write_json_atomic


def _journal_dir(store) -> Path:
    return store.state_dir / config.JOURNAL_DIRNAME


def _path(store, attempt_id: str, suffix: str) -> Path:
    if not config.OBJECT_ID_RE.match(attempt_id) or "/" in attempt_id or "\\" in attempt_id:
        raise StateError(
            "RECOVERY_JOURNAL_INVALID",
            "attempt id is not a safe journal file name",
            details={"attempt_id": attempt_id},
        )
    return _journal_dir(store) / f"{attempt_id}{suffix}"


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(
            "RECOVERY_JOURNAL_INVALID",
            "recovery journal entry is not readable JSON",
            details={"entry": path.name, "reason": exc.__class__.__name__},
        ) from exc
    if not isinstance(value, dict):
        raise StateError("RECOVERY_JOURNAL_INVALID", "recovery journal entry must be an object")
    return value


def write_intent(store, intent: dict) -> Path:
    attempt_id = str(intent.get("attempt_id", ""))
    path = _path(store, attempt_id, config.INTENT_SUFFIX)
    write_json_atomic(path, intent)
    return path


def read_intent(store, attempt_id: str) -> dict | None:
    path = _path(store, attempt_id, config.INTENT_SUFFIX)
    return _read(path) if path.is_file() else None


def intents(store) -> list[dict]:
    directory = _journal_dir(store)
    if not directory.is_dir():
        return []
    records = [
        _read(path)
        for path in sorted(directory.glob(f"*{config.INTENT_SUFFIX}"))
        if path.is_file()
    ]
    for record in records:
        if set(("attempt_id", "snapshot_id", "parent_snapshot_id", "payload_hash", "manifest", "commit", "attempt")) - set(record):
            raise StateError("RECOVERY_JOURNAL_INVALID", "publish intent is missing required fields")
        if not config.OBJECT_ID_RE.match(str(record["attempt_id"])):
            raise StateError("RECOVERY_JOURNAL_INVALID", "publish intent has an invalid attempt id")
    return records


def clear_intent(store, attempt_id: str) -> None:
    path = _path(store, attempt_id, config.INTENT_SUFFIX)
    if path.is_file():
        path.unlink()


def _resolved_path(store, attempt_id: str, snapshot_id: str) -> Path:
    directory = _journal_dir(store) / "resolved"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{attempt_id}.{snapshot_id}{config.RESOLUTION_SUFFIX}"


def write_resolution(store, resolution: dict) -> Path:
    attempt_id = str(resolution.get("attempt_id", ""))
    id_path = _path(store, attempt_id, config.RESOLUTION_SUFFIX)
    write_json_atomic(id_path, resolution)
    snapshot_id = resolution.get("snapshot_id")
    if snapshot_id:
        write_json_atomic(_resolved_path(store, attempt_id, str(snapshot_id)), resolution)
    return id_path


def read_resolution(store, attempt_id: str, snapshot_id: str | None = None) -> dict | None:
    path = (
        _resolved_path(store, attempt_id, str(snapshot_id))
        if snapshot_id
        else _path(store, attempt_id, config.RESOLUTION_SUFFIX)
    )
    return _read(path) if path.is_file() else None


def clear_resolution(store, attempt_id: str, snapshot_id: str | None = None) -> None:
    """Drop one consequence file, or all of an attempt's, before re-resolution."""
    target = _path(store, attempt_id, config.RESOLUTION_SUFFIX)
    if target.is_file():
        target.unlink()
    resolved_dir = _journal_dir(store) / "resolved"
    if resolved_dir.is_dir():
        pattern = f"{attempt_id}.{snapshot_id}{config.RESOLUTION_SUFFIX}" if snapshot_id else f"{attempt_id}.*{config.RESOLUTION_SUFFIX}"
        for path in resolved_dir.glob(pattern):
            path.unlink()


def resolutions(store) -> list[dict]:
    directory = _journal_dir(store)
    if not directory.is_dir():
        return []
    records = [
        _read(path)
        for path in sorted(directory.glob(f"*{config.RESOLUTION_SUFFIX}"))
        if path.is_file()
    ]
    for record in records:
        if record.get("route") not in config.RECOVERY_ROUTES:
            raise StateError("RECOVERY_JOURNAL_INVALID", "recovery resolution has an unknown route")
    return records
