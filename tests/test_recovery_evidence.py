"""Recovery refuses to guess when persisted evidence is missing or conflicting."""

import json

import pytest

from lakecommit.errors import StateError
from tests.recovery_helpers import SimulatedCrash, crash_after, new_bench, reopen


def _intent(bench):
    return json.loads(next(bench.store.journal_dir.glob("*.intent.json")).read_text(encoding="utf-8"))


def _crash_after_current(tmp_path, monkeypatch):
    bench = new_bench(tmp_path)
    crash_after(monkeypatch, bench.store, "publish_current")
    with pytest.raises(SimulatedCrash):
        bench.engine.submit(bench.bundle.attempts[0])
    return bench, _intent(bench)


def test_missing_snapshot_without_intent_is_a_stable_state_error(tmp_path, monkeypatch):
    bench, intent = _crash_after_current(tmp_path, monkeypatch)
    bench.store.remove_snapshot(intent["snapshot_id"])
    with pytest.raises(StateError) as caught:
        reopen(bench)
    assert caught.value.code == "CURRENT_POINTER_UNRESOLVED"


def test_conflicting_intent_manifest_is_rejected(tmp_path, monkeypatch):
    bench, intent = _crash_after_current(tmp_path, monkeypatch)
    path = next(bench.store.journal_dir.glob("*.intent.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["manifest"]["files"][0]["content_hash"] = "e" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StateError) as caught:
        reopen(bench)
    assert caught.value.code == "RECOVERY_EVIDENCE_CONFLICT"


def test_conflicting_published_snapshot_is_rejected(tmp_path, monkeypatch):
    bench, intent = _crash_after_current(tmp_path, monkeypatch)
    commit_path = bench.store.snapshots_dir / intent["snapshot_id"] / "commit.json"
    commit = json.loads(commit_path.read_text(encoding="utf-8"))
    commit["attempt_id"] = "a-forged-attempt"
    commit_path.write_text(json.dumps(commit), encoding="utf-8")
    with pytest.raises(StateError) as caught:
        reopen(bench)
    assert caught.value.code == "RECOVERY_EVIDENCE_CONFLICT"


def test_confirmed_idempotency_for_another_snapshot_is_rejected(tmp_path, monkeypatch):
    bench, intent = _crash_after_current(tmp_path, monkeypatch)
    bench.ledger.record_first(
        __import__("lakecommit.recovery_evidence", fromlist=["accepted_decision"]).accepted_decision(
            intent, []
        ),
        intent["payload_hash"],
    )
    bench.ledger.connection.execute(
        "UPDATE idempotency_cache SET first_snapshot_id='snap-other' WHERE first_attempt_id=?",
        (intent["attempt_id"],),
    )
    bench.ledger.connection.commit()
    with pytest.raises(StateError) as caught:
        reopen(bench)
    assert caught.value.code == "RECOVERY_EVIDENCE_CONFLICT"


def test_reachable_snapshot_without_ledger_evidence_blocks_new_commits(tmp_path):
    bench = new_bench(tmp_path)
    decision = bench.engine.submit(bench.bundle.attempts[0])
    bench.ledger.delete_snapshot_audit(decision.resulting_snapshot_id)
    bench.ledger.connection.execute(
        "DELETE FROM idempotency_cache WHERE first_snapshot_id=?", (decision.resulting_snapshot_id,)
    )
    bench.ledger.connection.commit()
    with pytest.raises(StateError) as caught:
        reopen(bench)
    assert caught.value.code == "RECOVERY_EVIDENCE_MISSING"


def test_recovery_conclusion_is_pinned_against_a_different_writer(tmp_path, monkeypatch):
    bench, _intent_value = _crash_after_current(tmp_path, monkeypatch)
    reopened = reopen(bench)
    stored = reopened.ledger.get_recovery(bench.bundle.attempts[0].attempt_id)
    assert stored["route"] == "complete"
    forged = bench.bundle.attempts[0].to_dict()
    forged["writer_id"] = "writer-other"
    with pytest.raises(StateError) as caught:
        reopened.engine.submit_document(forged)
    assert caught.value.code == "RECOVERY_EVIDENCE_CONFLICT"


def test_two_unconfirmed_intents_are_ambiguous(tmp_path, monkeypatch):
    bench, intent = _crash_after_current(tmp_path, monkeypatch)
    original = next(bench.store.journal_dir.glob("*.intent.json"))
    clone = json.loads(original.read_text(encoding="utf-8"))
    clone["attempt_id"] = "a-alpha-clone"
    clone["attempt"]["attempt_id"] = "a-alpha-clone"
    (bench.store.journal_dir / "a-alpha-clone.intent.json").write_text(json.dumps(clone))
    with pytest.raises(StateError) as caught:
        reopen(bench, dict(bench.bundle.protocol, recovery_route="rollback"))
    assert caught.value.code == "RECOVERY_AMBIGUOUS"
    assert intent["attempt_id"] in caught.value.details["attempt_ids"]
