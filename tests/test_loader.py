import pytest

from lakecommit import config, loader, validator
from lakecommit.errors import InputError, SchemaError

def test_bundle_loads_scenario_table_and_attempts(bundle):
    assert bundle.scenario_id == "add_files_accepted"
    assert bundle.table.table_name == "lake_sales_orders"
    assert bundle.initial_snapshot.snapshot_id == "snap-0000"
    assert len(bundle.initial_snapshot.files) == 4
    assert bundle.attempts[0].attempt_id == "a-alpha-001"
    assert bundle.attempts[0].operation_type == "add_files"
    assert bundle.protocol == {
        "allow_safe_rebase": True,
        "allow_idempotency_mismatch": False,
    }
    assert bundle.scenario_path.startswith("fixtures/scenarios/")

def test_load_json_reports_parse_location(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"a": }', encoding="utf-8")
    with pytest.raises(InputError) as caught:
        loader.load_json(path)
    assert caught.value.code == "JSON_PARSE_ERROR"
    assert caught.value.exit_code == 2
    assert "line 1" in caught.value.message

def test_load_jsonl_reports_line_number(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('{"ok": 1}\n{"bad"}\n', encoding="utf-8")
    with pytest.raises(InputError) as caught:
        loader.load_jsonl(path)
    assert caught.value.code == "JSONL_PARSE_ERROR"
    assert caught.value.location.endswith(":2")

def test_missing_file_is_stable_input_error(tmp_path):
    with pytest.raises(InputError) as caught:
        loader.load_json(tmp_path / "missing.json")
    assert caught.value.code == "FILE_NOT_FOUND"
    assert caught.value.exit_code == 2

def test_empty_attempt_file_is_rejected(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(InputError) as caught:
        loader.load_jsonl(path)
    assert caught.value.code == "ATTEMPTS_EMPTY"

def test_idempotency_mismatch_is_caught_by_strict_validation():
    columns = ("region",)
    docs = loader.load_jsonl(config.FIXTURES_DIR / "attempts" / "11_idempotency_mismatch.jsonl")
    with pytest.raises(SchemaError) as caught:
        validator.validate_attempts(docs, "attempts.jsonl", columns, check_idempotency=True)
    assert caught.value.code == "IDEMPOTENCY_PAYLOAD_MISMATCH"

def test_scenario_is_allowed_to_opt_into_runtime_mismatch_probe():
    bundle = loader.load_bundle(config.SCENARIOS_DIR / "11_idempotency_mismatch.yaml")
    assert bundle.protocol["allow_idempotency_mismatch"] is True
    assert len(bundle.attempts) == 2

def test_loader_never_uses_system_time(monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: (_ for _ in ()).throw(AssertionError("clock read")))
    bundle = loader.load_bundle(config.SCENARIOS_DIR / "03_safe_rebase_accepted.yaml")
    assert bundle.attempts[1].logical_time == "2024-01-01T00:03:10Z"
