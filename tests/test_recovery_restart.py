"""End-to-end restart semantics: recovery conclusion drives later submits."""

import pytest

from dataclasses import replace

from tests.recovery_helpers import (
    SimulatedCrash,
    attempt_with,
    crash_after,
    new_bench,
    reopen,
)


def _second_attempt(bench):
    value = attempt_with(
        bench,
        attempt_id="a-alpha-002",
        operation={
            "type": "add_files",
            "files": [
                {
                    "path": "data/region=ca/part-0002.parquet",
                    "partition": {"region": "ca"},
                    "size_bytes": 450,
                    "row_count": 35,
                    "content_hash": "d" * 64,
                }
            ],
        },
    )
    return replace(value, idempotency_key="idem-alpha-002")


def test_completed_intent_still_serves_first_result_and_later_commits(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    reopened = reopen(bench)
    published = reopened.store.current_snapshot_id()
    second = reopened.engine.submit(_second_attempt(bench))
    assert second.status == "accepted"
    assert reopened.store.current_snapshot_id() == second.resulting_snapshot_id
    assert reopened.store.is_ancestor(published, second.resulting_snapshot_id)
    replay = attempt_with(bench, attempt_id="a-alpha-001-final")
    assert reopened.engine.submit(replay).outcome == "replayed"
    assert len(reopened.store.chain()) == 3
    assert reopened.store.verify()["ok"] is True


def test_rolled_back_attempt_can_be_rejudged_against_its_original_base(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    protocol = dict(bench.bundle.protocol, recovery_route="rollback")
    reopened = reopen(bench, protocol)
    assert reopened.store.current_snapshot_id() == "snap-0000"
    decision = reopened.engine.submit(bench.bundle.attempts[0])
    assert decision.outcome == "applied"
    assert decision.resulting_snapshot_id == reopened.store.current_snapshot_id()
    assert len(reopened.store.chain()) == 2


def test_recovered_attempt_cannot_be_rewritten_by_a_different_operation(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    reopened = reopen(bench)
    published = reopened.store.current_snapshot_id()
    forged = bench.bundle.attempts[0].to_dict()
    forged["operation"] = {
        "type": "add_files",
        "files": [
            {
                "path": "data/region=us/part-0099.parquet",
                "partition": {"region": "us"},
                "size_bytes": 10,
                "row_count": 1,
                "content_hash": "8" * 64,
            }
        ],
    }
    decision = reopened.engine.submit_document(forged)
    assert decision.status == "rejected"
    assert decision.conflict_codes == ["IDEMPOTENCY_PAYLOAD_MISMATCH"]
    assert reopened.store.current_snapshot_id() == published
    assert len(reopened.store.chain()) == 2


def test_second_restart_does_not_reopen_the_same_decision(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    first = reopen(bench)
    assert first.engine.recoveries[0].route == "complete"
    second = reopen(first)
    assert second.engine.recoveries == []
    assert second.ledger.recovery_count() == 1
    assert second.store.current_snapshot_id() == first.store.current_snapshot_id()


def test_rollback_then_second_crash_recovery_is_independent(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    protocol = dict(bench.bundle.protocol, recovery_route="rollback")
    first = reopen(bench, protocol)
    assert first.engine.recoveries[0].route == "rollback"
    # Re-judge the same attempt and interrupt its new publication as well.
    monkeypatch.undo()
    crash_after(monkeypatch, first.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        first.engine.submit(bench.bundle.attempts[0])
    second = reopen(first)
    assert second.engine.recoveries[0].route == "complete"
    assert second.store.current_snapshot_id() == second.engine.recoveries[0].snapshot_id
    assert second.ledger.get_idempotency("writer-alpha", "idem-alpha-001")["first_snapshot_id"] == second.store.current_snapshot_id()
    assert len(second.store.chain()) == 2
    assert second.store.verify()["ok"] is True
