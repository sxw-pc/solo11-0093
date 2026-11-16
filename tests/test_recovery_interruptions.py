"""Interruption tests for the recoverable "publishing" commit state."""

import json

import pytest

from lakecommit.errors import StateError
from tests.recovery_helpers import (
    SimulatedCrash,
    attempt_with,
    crash_after,
    crash_before,
    new_bench,
    reopen,
)


def _intent(bench):
    path = next(bench.store.journal_dir.glob("*.intent.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def test_crash_before_snapshot_directory_completes_from_intent(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_before(monkeypatch, bench.store, "publish_prepared")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    intent = _intent(bench)
    assert not bench.store.has_snapshot(intent["snapshot_id"])
    assert bench.store.current_snapshot_id() == "snap-0000"
    reopened = reopen(bench)
    result = reopened.engine.recoveries[0]
    assert result.route == "complete"
    assert reopened.store.current_snapshot_id() == intent["snapshot_id"]
    assert reopened.store.has_snapshot(intent["snapshot_id"])
    assert reopened.ledger.get_idempotency("writer-alpha", "idem-alpha-001")["first_snapshot_id"] == intent["snapshot_id"]
    decision = reopened.engine.submit(bench.bundle.attempts[0])
    assert decision.status == "accepted"
    assert decision.outcome == "recovered"
    assert decision.resulting_snapshot_id == intent["snapshot_id"]
    assert decision.snapshot_created is False
    replay = attempt_with(bench, attempt_id="a-alpha-001-replay")
    assert reopened.engine.submit(replay).outcome == "replayed"
    assert len(reopened.store.chain()) == 2
    assert reopened.store.verify()["ok"] is True


def test_crash_before_snapshot_directory_can_roll_back_explicitly(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_before(monkeypatch, bench.store, "publish_prepared")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    intent = _intent(bench)
    protocol = dict(bench.bundle.protocol, recovery_route="rollback")
    reopened = reopen(bench, protocol)
    result = reopened.engine.recoveries[0]
    assert result.route == "rollback"
    assert reopened.store.current_snapshot_id() == "snap-0000"
    assert not reopened.store.has_snapshot(intent["snapshot_id"])
    assert reopened.ledger.get_idempotency("writer-alpha", "idem-alpha-001") is None
    decision = reopened.engine.submit(bench.bundle.attempts[0])
    assert decision.status == "accepted"
    assert decision.snapshot_created is True
    assert reopened.store.verify()["ok"] is True


def test_crash_after_snapshot_directory_keeps_snapshot_without_current(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_prepared")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    intent = _intent(bench)
    assert bench.store.has_snapshot(intent["snapshot_id"])
    assert bench.store.current_snapshot_id() == "snap-0000"
    reopened = reopen(bench)
    assert reopened.engine.recoveries[0].resolution == "completed"
    assert reopened.store.current_snapshot_id() == intent["snapshot_id"]
    replay = attempt_with(bench, attempt_id="a-alpha-001-retry")
    decision = reopened.engine.submit(replay)
    assert decision.outcome == "replayed"
    assert decision.resulting_snapshot_id == intent["snapshot_id"]
    assert len(reopened.store.chain()) == 2
    repeated = reopened.engine.submit(bench.bundle.attempts[0])
    assert repeated.outcome == "recovered"
    assert repeated.resulting_snapshot_id == intent["snapshot_id"]


def test_crash_after_current_pointer_switch_is_completed_once(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    published = bench.store.current_snapshot_id()
    assert published != "snap-0000"
    reopened = reopen(bench)
    assert reopened.engine.recoveries[0].route == "complete"
    assert reopened.store.current_snapshot_id() == published
    assert reopened.ledger.has_snapshot_audit(published)
    record = reopened.ledger.get_idempotency("writer-alpha", "idem-alpha-001")
    assert record["first_snapshot_id"] == published
    assert len(reopened.store.chain()) == 2


def test_crash_after_snapshot_audit_recovers_first_result(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.ledger, "audit_snapshot")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    published = bench.store.current_snapshot_id()
    assert published != "snap-0000"
    assert bench.ledger.get_idempotency("writer-alpha", "idem-alpha-001") is None
    reopened = reopen(bench)
    record = reopened.ledger.get_idempotency("writer-alpha", "idem-alpha-001")
    assert record is not None
    assert record["first_snapshot_id"] == published
    assert reopened.ledger.has_snapshot_audit(published)
    assert len(reopened.store.chain()) == 2


def test_crash_after_first_result_returns_stable_recovery_decision(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.ledger, "record_first")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    published = bench.store.current_snapshot_id()
    reopened = reopen(bench)
    assert reopened.engine.recoveries[0].resolution == "already_confirmed"
    record = reopened.ledger.get_idempotency("writer-alpha", "idem-alpha-001")
    assert record["first_snapshot_id"] == published
    assert len(reopened.store.chain()) == 2
    second = reopen(reopened)
    third = reopen(second)
    assert third.engine.recoveries == []
    assert third.store.current_snapshot_id() == published


def test_same_key_different_payload_still_conflicts_after_recovery(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    reopened = reopen(bench)
    changed = attempt_with(
        bench,
        attempt_id="a-alpha-001-other",
        operation={
            "type": "add_files",
            "files": [
                {
                    "path": "data/region=ca/part-0099.parquet",
                    "partition": {"region": "ca"},
                    "size_bytes": 10,
                    "row_count": 1,
                    "content_hash": "9" * 64,
                }
            ],
        },
    )
    decision = reopened.engine.submit(changed)
    assert decision.conflict_codes == ["IDEMPOTENCY_PAYLOAD_MISMATCH"]
    assert reopened.store.current_snapshot_id() != "snap-0000"
    assert len(reopened.store.chain()) == 2


def test_rollback_route_leaves_attempt_re_evaluable(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    protocol = dict(bench.bundle.protocol, recovery_route="rollback")
    reopened = reopen(bench, protocol)
    result = reopened.engine.recoveries[0]
    assert result.route == "rollback"
    assert reopened.store.current_snapshot_id() == "snap-0000"
    assert not reopened.store.has_snapshot(result.snapshot_id)
    assert reopened.ledger.get_idempotency("writer-alpha", "idem-alpha-001") is None
    decision = reopened.engine.submit(bench.bundle.attempts[0])
    assert decision.status == "accepted"
    assert decision.snapshot_created is True
    assert reopened.store.verify()["ok"] is True


def test_rollback_is_sticky_across_repeated_restarts(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    protocol = dict(bench.bundle.protocol, recovery_route="rollback")
    first = reopen(bench, protocol)
    resolved = first.engine.recoveries[0]
    assert resolved.route == "rollback"
    # A second restart asks for "complete"; the already-recorded route must win.
    second = reopen(first)
    assert second.engine.recoveries == []
    assert second.store.current_snapshot_id() == "snap-0000"
    assert second.ledger.get_recovery(resolved.attempt_id)["route"] == "rollback"


def test_rollback_refuses_when_current_advanced_past_the_intent(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    intent = _intent(bench)
    # Forge a later confirmed commit built on the unconfirmed one.
    second = attempt_with(bench, attempt_id="a-alpha-002")
    snapshot, commit = bench.store.commit_snapshot(
        files=bench.store.load_snapshot(intent["snapshot_id"]).files,
        attempt=second,
        payload_hash="p" * 64,
        base_snapshot_id=intent["snapshot_id"],
        touched_partitions=[],
        added_paths=[],
        removed_paths=[],
        rebase=None,
    )
    bench.ledger.audit_snapshot(snapshot, commit)
    bench.ledger.connection.execute(
        "INSERT INTO idempotency_cache VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            second.writer_id,
            second.idempotency_key,
            "p" * 64,
            second.attempt_id,
            "accepted",
            "applied",
            snapshot.snapshot_id,
            "[]",
            "{}",
            0,
        ),
    )
    bench.ledger.connection.commit()
    protocol = dict(bench.bundle.protocol, recovery_route="rollback")
    with pytest.raises(StateError) as caught:
        reopen(bench, protocol)
    assert caught.value.code == "RECOVERY_ROLLBACK_UNSAFE"
    assert bench.store.has_snapshot(intent["snapshot_id"])


def test_current_switch_crash_then_same_payload_yields_one_snapshot(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    published = bench.store.current_snapshot_id()
    reopened = reopen(bench)
    recovered = reopened.engine.submit(bench.bundle.attempts[0])
    assert recovered.outcome == "recovered"
    assert recovered.resulting_snapshot_id == published
    replay = attempt_with(bench, attempt_id="a-alpha-001-next")
    again = reopened.engine.submit(replay)
    assert again.outcome == "replayed"
    assert again.resulting_snapshot_id == published
    assert len(reopened.store.chain()) == 2


def test_confirmed_attempt_is_never_rolled_back(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.ledger, "record_first")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    published = bench.store.current_snapshot_id()
    protocol = dict(bench.bundle.protocol, recovery_route="rollback")
    reopened = reopen(bench, protocol)
    result = reopened.engine.recoveries[0]
    assert result.route == "complete"
    assert result.resolution == "already_confirmed"
    assert reopened.store.current_snapshot_id() == published
    assert reopened.store.has_snapshot(published)
    assert reopened.ledger.get_idempotency("writer-alpha", "idem-alpha-001")["first_snapshot_id"] == published


def test_snapshot_audit_crash_then_rollback_preserves_previous_snapshots(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.ledger, "audit_snapshot")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    published = bench.store.current_snapshot_id()
    protocol = dict(bench.bundle.protocol, recovery_route="rollback")
    reopened = reopen(bench, protocol)
    assert reopened.engine.recoveries[0].route == "rollback"
    assert reopened.store.current_snapshot_id() == "snap-0000"
    assert not reopened.store.has_snapshot(published)
    assert reopened.store.verify()["ok"] is True
