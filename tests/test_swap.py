"""Isolated tests for the atomic cross-partition swap_file_sets operation."""

from lakecommit import idempotency, validator
from lakecommit.errors import SchemaError
from lakecommit.models import Attempt
import pytest

US_OLD = "data/region=us/part-0001.parquet"
EU_OLD = "data/region=eu/part-0001.parquet"

def _file(path, region, digest):
    return {"path": path, "partition": {"region": region}, "size_bytes": 100,
            "row_count": 5, "content_hash": digest}

def _swap_doc(**overrides):
    op = dict(overrides.pop("operation", {}))
    for nested in ("swap_id", "authorization", "groups"):
        if nested in overrides:
            op[nested] = overrides.pop(nested)
    doc = {
        "attempt_id": "a-swap-test", "writer_id": "writer-swap",
        "idempotency_key": "idem-swap-test", "base_snapshot_id": "snap-0000",
        "logical_time": "2024-01-01T00:20:00Z",
        "operation": {
            "type": "swap_file_sets", "swap_id": "swap-gen-test",
            "authorization": "strict_partition",
            "groups": [
                {"partition": {"region": "us"}, "expected_files": [US_OLD],
                 "files": [_file("data/region=us/part-0040.parquet", "us", "a" * 64)]},
                {"partition": {"region": "eu"}, "expected_files": [EU_OLD],
                 "files": [_file("data/region=eu/part-0040.parquet", "eu", "e" * 64)]}],
        }}
    doc["operation"].update(op)
    doc.update(overrides)
    return doc

def _validate(doc):
    return validator.validate_attempt_document(doc, location="test", columns=("region",))

def _paths(snapshot):
    return {entry.path for entry in snapshot.files}

def _replace_us(bench, expected, new_path):
    return bench.engine.submit(Attempt(
        attempt_id="a-setup", writer_id="writer-setup", idempotency_key="idem-setup",
        base_snapshot_id="snap-0000", logical_time="2024-01-01T00:19:00Z",
        operation={"type": "replace_file_set", "expected_files": [expected],
                   "files": [_file(new_path, "us", "3" * 64)]}))

def _add_attempt(path, region):
    return Attempt(
        attempt_id="a-add", writer_id="writer-add", idempotency_key="idem-add",
        base_snapshot_id="snap-0000", logical_time="2024-01-01T00:19:30Z",
        operation={"type": "add_files", "files": [_file(path, region, "d" * 64)]})

def test_swap_publishes_one_snapshot_for_both_partitions(bench):
    decision = bench.engine.submit(_validate(_swap_doc()))
    assert decision.status == "accepted" and decision.snapshot_created is True
    assert len(bench.store.chain()) == 2
    commit = bench.store.load_commit(decision.resulting_snapshot_id)
    assert commit["operation_type"] == "swap_file_sets"
    assert commit["touched_partitions"] == ["region=eu", "region=us"]
    paths = _paths(bench.store.current_snapshot())
    assert {"data/region=us/part-0040.parquet", "data/region=eu/part-0040.parquet",
            "data/region=us/part-0002.parquet", "data/region=ca/part-0001.parquet"} <= paths
    assert US_OLD not in paths and EU_OLD not in paths
    assert bench.store.verify()["ok"] is True

def test_changed_expected_on_one_side_rejects_whole_swap_without_snapshot(bench):
    assert _replace_us(bench, US_OLD, "data/region=us/part-0003.parquet").status == "accepted"
    decision = bench.engine.submit(_validate(_swap_doc()))
    assert decision.status == "rejected" and decision.snapshot_created is False
    assert set(decision.conflict_codes) == {
        "FILE_SET_CHANGED_SINCE_BASE", "SWAP_PARTITION_TOUCHED_SINCE_BASE"}
    assert decision.retry_required is True
    assert decision.reasons[0]["details"]["paths"] == [US_OLD]
    assert len(bench.store.chain()) == 2

@pytest.mark.parametrize(("mutate", "code"), [
    (lambda d: d["operation"]["groups"][1]["files"][0].update(
        {"path": "data/region=us/part-0040.parquet"}), "DUP_FILE_PATH"),
    (lambda d: d["operation"]["groups"][1].update({"expected_files": [US_OLD]}),
     "DUP_EXPECTED_FILE"),
    (lambda d: [g.update({"partition": {"region": "us"}}) or
                g["files"][0].update({"partition": {"region": "us"}})
                for g in d["operation"]["groups"]], "SWAP_PARTITION_MISMATCH"),
    (lambda d: d["operation"].update({"authorization": "merge_anyway"}),
     "SWAP_AUTHORIZATION_INVALID"),
    (lambda d: d["operation"]["groups"][0]["files"][0].update(
        {"partition": {"region": "eu"}}), "SWAP_GROUP_FILE_MISMATCH"),
    (lambda d: d["operation"]["groups"][1]["files"][0].update({"path": EU_OLD}),
     "SWAP_PATH_OVERLAP")])
def test_swap_structural_validation(mutate, code):
    doc = _swap_doc()
    mutate(doc)
    with pytest.raises(SchemaError) as caught:
        _validate(doc)
    assert caught.value.code == code

def test_strict_route_rejects_unrelated_concurrent_append(bench):
    assert bench.engine.submit(_add_attempt(
        "data/region=us/part-0030.parquet", "us")).status == "accepted"
    decision = bench.engine.submit(_validate(_swap_doc()))
    assert decision.status == "rejected"
    assert decision.conflict_codes == ["SWAP_PARTITION_TOUCHED_SINCE_BASE"]
    assert decision.retry_required is True and len(bench.store.chain()) == 2

def test_preserve_route_keeps_unrelated_append_and_swaps_expected_sets(bench):
    assert bench.engine.submit(_add_attempt(
        "data/region=us/part-0030.parquet", "us")).status == "accepted"
    decision = bench.engine.submit(_validate(_swap_doc(authorization="preserve_unrelated")))
    assert decision.status == "accepted" and decision.rebase is not None
    assert decision.rebase["strategy"] == "SWAP_PARTITION_REBASE"
    assert decision.rebase["checked_partitions"] == ["region=eu", "region=us"]
    paths = _paths(bench.store.current_snapshot())
    assert "data/region=us/part-0030.parquet" in paths
    assert "data/region=us/part-0040.parquet" in paths
    assert "data/region=eu/part-0040.parquet" in paths and US_OLD not in paths
    assert len(bench.store.chain()) == 3

def test_related_concurrent_replacement_rejects_even_under_preserve(bench):
    assert _replace_us(bench, US_OLD, "data/region=us/part-0003.parquet").status == "accepted"
    decision = bench.engine.submit(_validate(_swap_doc(authorization="preserve_unrelated")))
    assert decision.status == "rejected"
    assert decision.conflict_codes == ["FILE_SET_CHANGED_SINCE_BASE"]
    assert decision.retry_required is True and len(bench.store.chain()) == 2

def test_rejected_swap_can_retry_on_fresh_snapshot(bench):
    setup = _replace_us(bench, US_OLD, "data/region=us/part-0003.parquet")
    assert bench.engine.submit(_validate(_swap_doc())).status == "rejected"
    retry = _swap_doc(attempt_id="a-swap-retry", idempotency_key="idem-swap-retry",
                      base_snapshot_id=setup.resulting_snapshot_id,
                      authorization="preserve_unrelated")
    group = retry["operation"]["groups"][0]
    group["expected_files"] = ["data/region=us/part-0003.parquet"]
    group["files"][0]["path"] = "data/region=us/part-0041.parquet"
    decision = bench.engine.submit(_validate(retry))
    assert decision.status == "accepted" and decision.rebase is None
    paths = _paths(bench.store.current_snapshot())
    assert "data/region=us/part-0003.parquet" not in paths
    assert "data/region=us/part-0041.parquet" in paths
    assert "data/region=eu/part-0040.parquet" in paths
    assert bench.store.verify()["ok"] is True

def test_swap_replay_returns_first_snapshot_without_new_one(bench):
    first = bench.engine.submit(_validate(_swap_doc()))
    replay = bench.engine.submit(_validate(_swap_doc(
        attempt_id="a-swap-replay", logical_time="2024-01-01T00:20:30Z")))
    assert replay.outcome == "replayed" and replay.snapshot_created is False
    assert replay.resulting_snapshot_id == first.resulting_snapshot_id
    assert replay.replay == {"replayed": True, "replay_of_attempt_id": "a-swap-test",
                             "replay_count": 1}
    assert len(bench.store.chain()) == 2

def test_swap_payload_identity_includes_swap_id_and_authorization():
    digest = idempotency.payload_hash(_validate(_swap_doc()))
    assert digest != idempotency.payload_hash(_validate(_swap_doc(swap_id="swap-gen-other")))
    assert digest != idempotency.payload_hash(
        _validate(_swap_doc(authorization="preserve_unrelated")))

def test_support_matrix_keeps_all_four_operations():
    assert validator.config.SUPPORTED_OPERATIONS == (
        "add_files", "replace_partition", "replace_file_set", "swap_file_sets")

def test_preserve_route_respects_scenario_rebase_toggle(bench):
    bench.engine.allow_safe_rebase = False
    assert bench.engine.submit(_add_attempt(
        "data/region=us/part-0030.parquet", "us")).status == "accepted"
    decision = bench.engine.submit(_validate(_swap_doc(
        attempt_id="a-norebase", idempotency_key="idem-norebase",
        authorization="preserve_unrelated")))
    assert decision.status == "rejected"
    assert decision.conflict_codes == ["STALE_BASE_REBASE_DISABLED"]
    assert decision.retry_required is True

def test_evaluator_rejects_expected_file_from_other_partition():
    from lakecommit.models import FileEntry, Snapshot
    from lakecommit.swap import evaluate_swap

    def fe(path, region, digest):
        return FileEntry(path=path, partition={"region": region}, size_bytes=1,
                         row_count=1, content_hash=digest)
    base = Snapshot("snap-0000", None, 0, "2024-01-01T00:00:00Z",
                    (fe("d/us/a.parquet", "us", "a" * 64),
                     fe("d/eu/b.parquet", "eu", "b" * 64)))
    operation = {"type": "swap_file_sets", "swap_id": "swap-x",
                 "authorization": "preserve_unrelated", "groups": [
        {"partition": {"region": "us"}, "expected_files": ["d/eu/b.parquet"],
         "files": [fe("d/us/new.parquet", "us", "c" * 64).to_dict()]},
        {"partition": {"region": "eu"}, "expected_files": ["d/us/a.parquet"],
         "files": [fe("d/eu/new.parquet", "eu", "d" * 64).to_dict()]}]}
    attempt = Attempt("a1", "w1", "k1", "snap-0000", "2024-01-01T00:01:00Z", operation)
    result = evaluate_swap(attempt, base, base, [], ("region",), True)
    assert result.accepted is False
    assert "SWAP_EXPECTED_PARTITION_MISMATCH" in result.conflict_codes
