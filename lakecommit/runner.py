"""Scenario execution, artifact writing, explain and golden verification."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from lakecommit import config, loader, report_html, report_json, report_md
from lakecommit.errors import GoldenMismatchError, InputError, StateError
from lakecommit.fingerprint import canonical_json, write_json_atomic
from lakecommit.ledger import Ledger
from lakecommit.protocol import ProtocolEngine
from lakecommit.store import SnapshotStore


def prepare_workdir(workdir: str | Path) -> Path:
    path = Path(workdir)
    if path.exists() and not path.is_dir():
        raise InputError("WORKDIR_NOT_DIRECTORY", "workdir exists and is not a directory")
    path.mkdir(parents=True, exist_ok=True)
    state_dir = path / config.STATE_DIRNAME
    if state_dir.exists():
        shutil.rmtree(state_dir)
    for name in (config.REPORT_JSON, config.REPORT_MD, config.REPORT_HTML, config.RUN_META):
        candidate = path / name
        if candidate.is_file():
            candidate.unlink()
    return path


def _write_run_meta(workdir: Path, bundle) -> None:
    meta = {
        "schema_version": config.SCHEMA_VERSION,
        "scenario_id": bundle.scenario_id,
        "scenario_name": bundle.name,
        "golden": f"fixtures/expected/{bundle.scenario_id}.json",
        "artifacts": [config.REPORT_JSON, config.REPORT_MD, config.REPORT_HTML],
    }
    write_json_atomic(workdir / config.RUN_META, meta)


def run_scenario(
    scenario_path: str | Path,
    workdir: str | Path,
    *,
    reset: bool = True,
) -> dict:
    bundle = loader.load_bundle(scenario_path)
    workdir_path = Path(workdir).resolve()
    if reset:
        prepare_workdir(workdir_path)
    else:
        workdir_path.mkdir(parents=True, exist_ok=True)

    store = SnapshotStore(workdir_path)
    if reset:
        store.reset()
    ledger = Ledger(store.ledger_path, reset=True)
    try:
        initial = store.initialize(bundle.initial_snapshot, bundle.table.partition_columns)
        ledger.audit_snapshot(initial, store.load_commit(initial.snapshot_id))
        engine = ProtocolEngine(bundle.table, store, ledger, bundle.protocol)
        decisions = [engine.submit(attempt) for attempt in bundle.attempts]
        verification = store.verify()
        report = report_json.build_report(bundle, decisions, store, verification)
        report_json.write_json_report(workdir_path, report)
        report_md.write_markdown_report(workdir_path, report)
        report_html.write_html_report(workdir_path, report)
        _write_run_meta(workdir_path, bundle)
    finally:
        ledger.close()
    if not verification["ok"]:
        raise StateError(
            "STATE_VERIFICATION_FAILED",
            "snapshot state verification failed; inspect state_verification in report.json",
        )
    return report


def verify_golden(workdir: str | Path) -> dict:
    workdir_path = Path(workdir)
    report_path = workdir_path / config.REPORT_JSON
    if not report_path.is_file():
        raise InputError(
            "REPORT_NOT_FOUND",
            "workdir does not contain report.json",
            location=str(config.REPORT_JSON),
        )
    report = report_json.load_report(workdir_path)
    scenario_id = str(report.get("scenario", {}).get("id", ""))
    golden_path = config.EXPECTED_DIR / f"{scenario_id}.json"
    if not golden_path.is_file():
        raise GoldenMismatchError(
            "GOLDEN_NOT_FOUND",
            "no golden result exists for this scenario id",
            details={"scenario_id": scenario_id, "golden": golden_path.name},
        )
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    if canonical_json(report) != canonical_json(expected):
        differing = sorted(
            key
            for key in set(report) | set(expected)
            if report.get(key) != expected.get(key)
        )
        raise GoldenMismatchError(
            "GOLDEN_MISMATCH",
            "normalized report differs from the committed golden result",
            details={
                "scenario_id": scenario_id,
                "golden": golden_path.name,
                "report_fingerprint": report.get("run_fingerprint"),
                "golden_fingerprint": expected.get("run_fingerprint"),
                "differing_fields": differing,
            },
        )
    return {
        "scenario_id": scenario_id,
        "golden": golden_path.name,
        "match": True,
        "run_fingerprint": report["run_fingerprint"],
    }


def explain_attempt(workdir: str | Path, attempt_id: str) -> dict:
    store = SnapshotStore(Path(workdir))
    if not store.ledger_path.is_file():
        raise InputError("LEDGER_NOT_FOUND", "workdir does not contain a ledger")
    try:
        with Ledger(store.ledger_path, read_only=True) as ledger:
            attempt = ledger.get_attempt(attempt_id)
    except Exception as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError(
            "LEDGER_READ_ERROR",
            "audit ledger could not be read",
            details={"reason": exc.__class__.__name__},
        ) from exc
    if attempt is None:
        raise InputError(
            "ATTEMPT_NOT_FOUND",
            "attempt id is not present in the audit ledger",
            details={"attempt_id": attempt_id},
        )
    return attempt


def list_scenarios() -> list[Path]:
    return sorted(config.SCENARIOS_DIR.glob("*.yaml"))
