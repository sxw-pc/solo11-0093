from lakecommit import config, loader


def _attempts(name):
    return loader.load_bundle(config.SCENARIOS_DIR / name).attempts


def test_mixed_workload_covers_accept_rebase_conflict_replay(bench):
    decisions = [bench.engine.submit(attempt) for attempt in _attempts("13_mixed_workload.yaml")]
    assert [item.status for item in decisions] == [
        "accepted", "accepted", "accepted", "rejected", "rejected", "accepted", "rejected",
    ]
    assert [item.outcome for item in decisions] == [
        "applied", "applied", "applied", "rejected", "rejected", "replayed", "rejected",
    ]
    assert decisions[1].rebase is not None
    assert decisions[1].rebase["current_snapshot_id"] == decisions[0].resulting_snapshot_id
    assert decisions[1].rebase["base_snapshot_id"] == "snap-0000"
    assert "REPLACE_PARTITION_CONFLICT" in decisions[3].conflict_codes
    assert decisions[3].retry_required is True
    assert decisions[4].conflict_codes == ["BASE_SNAPSHOT_UNKNOWN"]
    assert decisions[5].replay["replayed"] is True
    assert decisions[5].resulting_snapshot_id == decisions[0].resulting_snapshot_id
    assert decisions[6].conflict_codes == ["IDEMPOTENCY_PAYLOAD_MISMATCH"]
    assert len(bench.store.chain()) == 4
    assert bench.store.verify()["ok"] is True


def test_duplicate_path_is_rejected_without_snapshot(bench):
    decision = bench.engine.submit(_attempts("02_duplicate_path_rejected.yaml")[0])
    assert decision.status == "rejected"
    assert decision.conflict_codes == ["FILE_PATH_DUPLICATE"]
    assert decision.snapshot_created is False
    assert bench.store.current_snapshot_id() == "snap-0000"
    assert decision.retry_required is False


def test_replace_partition_collision_is_rejected(bench):
    decision = bench.engine.submit(_attempts("06_replace_collision_rejected.yaml")[0])
    assert decision.status == "rejected"
    assert decision.conflict_codes == ["FILE_PATH_DUPLICATE"]


def test_file_set_replacement_accepts_explicit_files(bench):
    accepted = bench.engine.submit(_attempts("07_file_set_accepted.yaml")[0])
    assert accepted.status == "accepted"
    assert accepted.resulting_snapshot_id
    assert "data/region=us/part-0009.parquet" in {
        entry.path for entry in bench.store.current_snapshot().files
    }


def test_file_set_replacement_detects_changed_expected_file(bench):
    attempts = _attempts("08_file_set_changed_conflict.yaml")
    assert bench.engine.submit(attempts[0]).status == "accepted"
    rejected = bench.engine.submit(attempts[1])
    assert rejected.status == "rejected"
    assert rejected.conflict_codes == ["FILE_SET_CHANGED_SINCE_BASE"]
    assert rejected.retry_required is True


def test_rejected_attempt_can_retry_on_fresh_snapshot(bench):
    decisions = [bench.engine.submit(item) for item in _attempts("12_reject_then_retry.yaml")]
    assert [item.status for item in decisions] == ["accepted", "rejected", "accepted"]
    assert decisions[1].retry_required is True
    assert decisions[2].attempt.retry_of_attempt_id == "a-beta-012"
    assert decisions[2].rebase is None
    assert bench.store.current_snapshot_id() == decisions[2].resulting_snapshot_id
    assert bench.store.verify()["ok"] is True


def test_stale_add_to_touched_partition_is_rejected(bench):
    decisions = [bench.engine.submit(item) for item in _attempts("14_add_partition_conflict.yaml")]
    assert decisions[0].status == "accepted"
    assert decisions[1].conflict_codes == ["PARTITION_TOUCHED_SINCE_BASE"]
    assert decisions[1].retry_required is True
