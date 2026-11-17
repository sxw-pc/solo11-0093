from dataclasses import replace

from lakecommit import idempotency
from lakecommit.ledger import Ledger
from lakecommit.protocol import ProtocolEngine


def test_payload_hash_excludes_transport_fields_but_includes_base_and_operation(bench):
    attempt = bench.bundle.attempts[0]
    moved = replace(attempt, attempt_id="other-id", logical_time="2024-02-01T00:00:00Z")
    assert idempotency.payload_hash(attempt) == idempotency.payload_hash(moved)
    other_base = replace(attempt, base_snapshot_id="snap-other")
    assert idempotency.payload_hash(attempt) != idempotency.payload_hash(other_base)
    other_operation = replace(attempt, operation={"type": "add_files", "files": []})
    assert idempotency.payload_hash(attempt) != idempotency.payload_hash(other_operation)


def test_replay_returns_first_snapshot_without_creating_another(bench):
    attempt = bench.bundle.attempts[0]
    first = bench.engine.submit(attempt)
    replay = replace(
        attempt,
        attempt_id="a-alpha-001-replay",
        logical_time="2024-01-01T00:01:30Z",
    )
    second = bench.engine.submit(replay)
    assert first.outcome == "applied"
    assert second.outcome == "replayed"
    assert second.status == "accepted"
    assert second.resulting_snapshot_id == first.resulting_snapshot_id
    assert second.snapshot_created is False
    assert second.replay == {
        "replayed": True,
        "replay_of_attempt_id": "a-alpha-001",
        "replay_count": 1,
    }
    assert len(bench.store.chain()) == 2
    assert any(item["code"] == "IDEMPOTENT_REPLAY" for item in second.reasons)


def test_same_key_different_payload_is_rejected_and_does_not_replace_first(bench):
    attempt = bench.bundle.attempts[0]
    first = bench.engine.submit(attempt)
    document = attempt.to_dict()
    document["attempt_id"] = "a-alpha-001-wrong"
    document["logical_time"] = "2024-01-01T00:01:40Z"
    document["operation"] = {
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
    }
    rejected = bench.engine.submit_document(document)
    assert rejected.status == "rejected"
    assert rejected.conflict_codes == ["IDEMPOTENCY_PAYLOAD_MISMATCH"]
    assert rejected.resulting_snapshot_id is None
    assert bench.store.current_snapshot_id() == first.resulting_snapshot_id
    record = bench.ledger.get_idempotency(attempt.writer_id, attempt.idempotency_key)
    assert record["first_attempt_id"] == attempt.attempt_id
    assert record["payload_hash"] == first.payload_hash


def test_idempotency_cache_survives_new_engine_and_ledger_connection(bench):
    attempt = bench.bundle.attempts[0]
    first = bench.engine.submit(attempt)
    other_ledger = Ledger(bench.store.ledger_path)
    try:
        other_engine = ProtocolEngine(
            bench.bundle.table, bench.store, other_ledger, bench.bundle.protocol
        )
        replay = replace(
            attempt,
            attempt_id="a-alpha-001-after-restart",
            logical_time="2024-01-01T00:01:50Z",
        )
        decision = other_engine.submit(replay)
        assert decision.outcome == "replayed"
        assert decision.resulting_snapshot_id == first.resulting_snapshot_id
        assert other_ledger.idempotency_count() == 1
        assert other_ledger.get_idempotency(attempt.writer_id, attempt.idempotency_key)["replay_count"] == 1
    finally:
        other_ledger.close()
    assert len(bench.store.chain()) == 2
