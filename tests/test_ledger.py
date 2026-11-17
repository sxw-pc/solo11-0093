import json

from lakecommit.ledger import Ledger


def test_ledger_records_attempt_decision_and_snapshot_audit(bench):
    decision = bench.engine.submit(bench.bundle.attempts[0])
    rows = bench.ledger.list_attempts()
    assert len(rows) == 1
    assert rows[0]["attempt_id"] == "a-alpha-001"
    assert rows[0]["status"] == "accepted"
    assert rows[0]["operation"]["type"] == "add_files"
    assert rows[0]["reasons"][0]["code"] == "ACCEPTED_ON_CURRENT_BASE"
    assert rows[0]["resulting_snapshot_id"] == decision.resulting_snapshot_id
    assert rows[0]["replay_of_attempt_id"] is None
    assert rows[0]["replay_count"] == 0
    snapshots = bench.ledger.connection.execute(
        "SELECT snapshot_id, sequence, operation_type FROM snapshot_audit ORDER BY sequence"
    ).fetchall()
    assert [(row["snapshot_id"], row["sequence"]) for row in snapshots] == [
        ("snap-0000", 0),
        (decision.resulting_snapshot_id, 1),
    ]
    assert snapshots[1]["operation_type"] == "add_files"


def test_idempotency_cache_is_serialized_and_replay_count_is_persisted(bench):
    attempt = bench.bundle.attempts[0]
    first = bench.engine.submit(attempt)
    record = bench.ledger.get_idempotency(attempt.writer_id, attempt.idempotency_key)
    assert record["first_snapshot_id"] == first.resulting_snapshot_id
    stored = json.loads(record["first_decision_json"])
    assert stored["attempt_id"] == attempt.attempt_id
    assert stored["replay"] is None
    assert bench.ledger.attempt_count() == 1
    assert bench.ledger.idempotency_count() == 1


def test_ledger_can_be_reopened_read_only(bench):
    decision = bench.engine.submit(bench.bundle.attempts[0])
    reader = Ledger(bench.store.ledger_path, read_only=True)
    try:
        row = reader.get_attempt("a-alpha-001")
        assert row["resulting_snapshot_id"] == decision.resulting_snapshot_id
        assert reader.get_attempt("missing") is None
        assert reader.attempt_count() == 1
        assert reader.idempotency_count() == 1
    finally:
        reader.close()


def test_ledger_schema_contains_no_wall_clock_columns(bench):
    bench.engine.submit(bench.bundle.attempts[0])
    for table in ("attempt_audit", "idempotency_cache", "snapshot_audit"):
        columns = {
            row["name"]
            for row in bench.ledger.connection.execute(f"PRAGMA table_info({table})")
        }
        assert "created_at" not in columns
        assert "timestamp" not in columns
        assert "system_time" not in columns
