"""Stable project constants and small path helpers."""

from __future__ import annotations

import re
from pathlib import Path

SCHEMA_VERSION = "1.0"
ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "fixtures"
SCENARIOS_DIR = FIXTURES_DIR / "scenarios"
EXPECTED_DIR = FIXTURES_DIR / "expected"
DEMO_SCENARIO = SCENARIOS_DIR / "13_mixed_workload.yaml"
DEFAULT_WORKDIR = Path(".lakecommit-demo")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

STATE_DIRNAME = "state"
SNAPSHOTS_DIRNAME = "snapshots"
LEDGER_FILENAME = "ledger.sqlite3"
CURRENT_FILENAME = "current"
REPORT_JSON = "report.json"
REPORT_MD = "report.md"
REPORT_HTML = "report.html"
RUN_META = "run.json"

ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SUPPORTED_OPERATIONS = ("add_files", "replace_partition", "replace_file_set")

TABLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
COLUMN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
OBJECT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
PARTITION_VALUE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
CONTENT_HASH_RE = re.compile(r"^[A-Za-z0-9:_-]{8,128}$")
LOGICAL_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
FILE_FIELDS = ("path", "partition", "size_bytes", "row_count", "content_hash")


def stable_location(path: Path) -> str:
    """Return a deterministic, non-sensitive display location for errors."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return path.name


def resolve_fixture_reference(scenario_path: Path, value: object, field: str) -> Path:
    """Resolve a scenario-local fixture reference without allowing URLs."""
    from lakecommit.errors import SchemaError

    if not isinstance(value, str) or not value.strip():
        raise SchemaError(
            "FIELD_REQUIRED",
            f"{field} must be a non-empty relative path",
            location=stable_location(scenario_path),
            field_path=field,
        )
    text = value.strip()
    if "://" in text:
        raise SchemaError(
            "PATH_URL",
            "fixture references may not be URLs",
            location=stable_location(scenario_path),
            field_path=field,
        )
    candidate = Path(text)
    if candidate.is_absolute():
        raise SchemaError(
            "PATH_ABSOLUTE",
            "fixture references must be relative",
            location=stable_location(scenario_path),
            field_path=field,
        )
    return (scenario_path.parent / candidate).resolve()


def default_protocol() -> dict:
    return {"allow_safe_rebase": True, "allow_idempotency_mismatch": False}
