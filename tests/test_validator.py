import copy

import pytest

from lakecommit import config, loader, validator
from lakecommit.errors import SchemaError

@pytest.fixture
def table_doc():
    return copy.deepcopy(loader.load_json(config.FIXTURES_DIR / "tables" / "lake_sales_orders.json"))

def _table_error(doc):
    with pytest.raises(SchemaError) as caught:
        validator.validate_table_document(doc, location="table.json")
    return caught.value

def test_table_document_produces_sorted_files_and_hash(table_doc):
    table, snapshot = validator.validate_table_document(table_doc)
    assert [entry.path for entry in snapshot.files] == sorted(entry.path for entry in snapshot.files)
    assert len(snapshot.manifest_hash) == 64
    assert table.partition_columns == ("region",)

@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/absolute.parquet", "PATH_ABSOLUTE"),
        ("data/../escape.parquet", "PATH_TRAVERSAL"),
        ("https://example.test/file.parquet", "PATH_URL"),
        ("C:/drive/file.parquet", "PATH_INVALID"),
        ("data//double.parquet", "PATH_INVALID"),
    ],
)
def test_file_paths_reject_unsafe_forms(table_doc, path, code):
    table_doc["initial_snapshot"]["files"][0]["path"] = path
    error = _table_error(table_doc)
    assert error.code == code
    assert error.field_path.startswith("initial_snapshot.files[0].path")

def test_partition_columns_must_match(table_doc):
    table_doc["initial_snapshot"]["files"][0]["partition"] = {"country": "ca"}
    error = _table_error(table_doc)
    assert error.code == "PARTITION_MISMATCH"

def test_file_metrics_must_be_non_negative_integers(table_doc):
    table_doc["initial_snapshot"]["files"][0]["size_bytes"] = -1
    assert _table_error(table_doc).code == "NON_NEGATIVE_INTEGER_REQUIRED"

def test_duplicate_initial_paths_are_rejected(table_doc):
    files = table_doc["initial_snapshot"]["files"]
    files[1]["path"] = files[0]["path"]
    error = _table_error(table_doc)
    assert error.code == "DUP_FILE_PATH"
    assert error.details["paths"]

def test_attempt_ids_must_be_unique():
    docs = loader.load_jsonl(config.FIXTURES_DIR / "attempts" / "01_add_files.jsonl")
    with pytest.raises(SchemaError) as caught:
        validator.validate_attempts(docs * 2, "attempts.jsonl", ("region",))
    assert caught.value.code == "DUP_ATTEMPT_ID"

def test_unsupported_operation_is_rejected():
    docs = loader.load_jsonl(config.FIXTURES_DIR / "attempts" / "01_add_files.jsonl")
    docs[0]["operation"]["type"] = "delete_rows"
    with pytest.raises(SchemaError) as caught:
        validator.validate_attempts(docs, "attempts.jsonl", ("region",))
    assert caught.value.code == "UNSUPPORTED_OPERATION"

def test_replace_partition_files_must_share_target_partition():
    docs = loader.load_jsonl(config.FIXTURES_DIR / "attempts" / "05_replace_partition.jsonl")
    docs[0]["operation"]["files"][1]["partition"]["region"] = "us"
    with pytest.raises(SchemaError) as caught:
        validator.validate_attempts(docs, "attempts.jsonl", ("region",))
    assert caught.value.code == "REPLACE_PARTITION_FILE_MISMATCH"

def test_invalid_protocol_boolean_is_schema_error():
    scenario = {
        "id": "x",
        "name": "x",
        "table": "tables/x.json",
        "attempts": "attempts/x.jsonl",
        "protocol": {"allow_safe_rebase": "yes"},
    }
    with pytest.raises(SchemaError) as caught:
        validator.validate_scenario_document(scenario, "scenario.yaml")
    assert caught.value.code == "FIELD_TYPE"

def test_retry_reference_must_exist():
    docs = loader.load_jsonl(config.FIXTURES_DIR / "attempts" / "12_reject_retry.jsonl")
    docs[2]["retry_of_attempt_id"] = "missing-attempt"
    with pytest.raises(SchemaError) as caught:
        validator.validate_attempts(docs, "attempts.jsonl", ("region",))
    assert caught.value.code == "RETRY_ATTEMPT_UNKNOWN"
