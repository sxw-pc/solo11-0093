"""Reports and explain expose the persisted recovery conclusion."""

import json

import pytest

from lakecommit import config, report_json, runner
from lakecommit.errors import InputError
from tests.recovery_helpers import SimulatedCrash, crash_after, new_bench, reopen


def test_report_contains_recovery_rows_with_evidence(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    reopened = reopen(bench)
    verification = reopened.store.verify()
    report = report_json.build_report(
        bench.bundle,
        [],
        reopened.store,
        verification,
        [
            {
                "attempt_id": record["attempt_id"],
                "route": record["route"],
                "resolution": record["resolution"],
                "snapshot_id": record["snapshot_id"],
                "parent_snapshot_id": record["parent_snapshot_id"],
                "payload_hash": record["payload_hash"],
                "evidence": record["evidence"],
                "reasons": record["reasons"],
            }
            for record in reopened.ledger.recovery_records()
        ],
    )
    assert report["recoveries"][0]["route"] == "complete"
    assert report["recoveries"][0]["evidence"]["snapshot_published"] is True
    assert report["recoveries"][0]["reasons"][-1]["code"] == "RECOVERY_COMPLETED"
    assert report["run_fingerprint"] == __import__(
        "lakecommit.fingerprint", fromlist=["object_fingerprint"]
    ).object_fingerprint({k: v for k, v in report.items() if k != "run_fingerprint"})


def test_explain_surfaces_recovery_evidence(tmp_path, monkeypatch):
    workdir = tmp_path / "run"
    runner.run_scenario(config.SCENARIOS_DIR / "01_add_files_accepted.yaml", workdir)
    bench = new_bench(tmp_path / "crash")
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    reopen(bench)
    explained = runner.explain_attempt(bench.workdir, "a-alpha-001")
    assert explained["recovery"]["route"] == "complete"
    assert explained["recovery"]["resolution"] == "completed"
    assert explained["status"] == "accepted"


def test_explain_reports_recovery_only_attempt(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.ledger, "record_first")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    reopen(bench)
    bench.ledger.close()
    with pytest.raises(InputError) as caught:
        runner.explain_attempt(bench.workdir, "missing-attempt")
    assert caught.value.code == "ATTEMPT_NOT_FOUND"


def test_runner_reopen_reuses_recovery_and_keeps_verify_ok(tmp_path, monkeypatch):
    from lakecommit import config as config_module

    bench = new_bench(tmp_path / "bench")
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    bench.ledger.close()
    report = runner.run_scenario(
        config_module.SCENARIOS_DIR / "01_add_files_accepted.yaml",
        bench.workdir,
        reset=False,
    )
    assert report["recoveries"][0]["route"] == "complete"
    assert report["attempts"][0]["outcome"] == "recovered"
    assert report["state_verification"]["ok"] is True
