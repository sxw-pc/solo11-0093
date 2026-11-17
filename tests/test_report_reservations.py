import json
from pathlib import Path
from lakecommit.report_reservations import preview_reservations
from test_query_rows import make_store, FIXTURES


def test_reservation_files_are_committed_and_schema_known(tmp_path):
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/report_reservations/intake.json").read_text())
    store,schemas=make_store(tmp_path)
    snapshot=store.load_snapshot(data["snapshot_id"])
    committed={f.path for f in snapshot.files}
    assert {p for r in data["requests"] for p in r["file_paths"]}==committed==set(schemas)
    assert all((FIXTURES/p).is_file() for p in committed)


def test_local_slot_first_preview_drops_shareable_jobs():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/report_reservations/intake.json").read_text())
    assert preview_reservations(data,"certified_close")["placed"]=={"qA":1,"qC":2}
    assert preview_reservations(data,"exploratory_quick")["placed"]=={"qA":1,"qC":2}


def test_qd_has_two_authorized_service_obligations():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/report_reservations/intake.json").read_text())
    assert data["contracts"]["certified_close"]["qD_delivery"]=="audited_slot_2_only"
    assert data["contracts"]["exploratory_quick"]["qD_delivery"]=="provisional_slot_1_allowed"
