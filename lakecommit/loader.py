"""Local YAML/JSON/JSONL loading for scenarios, tables and attempts."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from lakecommit import config, validator
from lakecommit.errors import InputError, SchemaError
from lakecommit.models import Attempt, ScenarioBundle, Snapshot, TableSpec


def _read_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        raise InputError(
            "FILE_NOT_FOUND",
            "input file does not exist",
            location=config.stable_location(path),
            field_path="$",
        )
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(
            "FILE_READ_ERROR",
            f"cannot read input file: {exc.__class__.__name__}",
            location=config.stable_location(path),
            field_path="$",
        ) from exc


def load_yaml(path: Path) -> object:
    text = _read_text(path)
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InputError(
            "YAML_PARSE_ERROR",
            f"invalid YAML: {exc.__class__.__name__}",
            location=config.stable_location(path),
            field_path="$",
        ) from exc


def load_json(path: Path) -> object:
    text = _read_text(path)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(
            "JSON_PARSE_ERROR",
            f"invalid JSON at line {exc.lineno}, column {exc.colno}",
            location=config.stable_location(path),
            field_path="$",
        ) from exc


def load_jsonl(path: Path) -> list[object]:
    text = _read_text(path)
    documents: list[object] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            documents.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise InputError(
                "JSONL_PARSE_ERROR",
                f"invalid JSONL at line {line_number}, column {exc.colno}",
                location=f"{config.stable_location(path)}:{line_number}",
                field_path="$",
            ) from exc
    if not documents:
        raise InputError(
            "ATTEMPTS_EMPTY",
            "attempts JSONL contains no records",
            location=config.stable_location(path),
            field_path="$",
        )
    return documents


def load_table(path: Path) -> tuple[TableSpec, Snapshot]:
    document = load_json(path)
    location = config.stable_location(path)
    if not isinstance(document, dict):
        raise SchemaError("TABLE_INVALID", "table document must be an object", location=location, field_path="$")
    return validator.validate_table_document(document, location=location)


def load_attempts(
    path: Path,
    partition_columns: tuple[str, ...],
    *,
    check_idempotency: bool = True,
) -> list[Attempt]:
    documents = load_jsonl(path)
    return validator.validate_attempts(
        documents,
        location=config.stable_location(path),
        columns=partition_columns,
        check_idempotency=check_idempotency,
    )


def load_bundle(scenario_path: str | Path) -> ScenarioBundle:
    path = Path(scenario_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    raw_scenario = load_yaml(path)
    scenario = validator.validate_scenario_document(
        raw_scenario, location=config.stable_location(path)
    )
    table_path = config.resolve_fixture_reference(path, scenario["table"], "table")
    attempts_path = config.resolve_fixture_reference(path, scenario["attempts"], "attempts")
    table, initial_snapshot = load_table(table_path)
    attempts = load_attempts(
        attempts_path,
        table.partition_columns,
        check_idempotency=not scenario["protocol"]["allow_idempotency_mismatch"],
    )
    return ScenarioBundle(
        scenario_id=scenario["id"],
        name=scenario["name"],
        description=scenario["description"],
        scenario_path=config.stable_location(path),
        table=table,
        initial_snapshot=initial_snapshot,
        attempts=attempts,
        protocol=scenario["protocol"],
    )
