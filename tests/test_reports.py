import json

from lakecommit import config
from lakecommit.fingerprint import object_fingerprint

def test_all_three_report_artifacts_are_written_and_self_contained(run_workdir):
    report_text = (run_workdir / config.REPORT_JSON).read_text(encoding="utf-8")
    markdown = (run_workdir / config.REPORT_MD).read_text(encoding="utf-8")
    html = (run_workdir / config.REPORT_HTML).read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["scenario"]["id"] == "add_files_accepted"
    assert report["summary"]["accepted"] == 1
    assert report["final_snapshot"]["file_count"] == 5
    assert report["attempts"][0]["status"] == "accepted"
    assert report["state_verification"]["ok"] is True
    assert len(report["run_fingerprint"]) == 64
    payload = dict(report)
    fingerprint = payload.pop("run_fingerprint")
    assert object_fingerprint(payload) == fingerprint
    for heading in ("# Lake commit verification", "## Summary", "## Snapshot chain", "## Final files"):
        assert heading in markdown
    assert "http://" not in html
    assert "https://" not in html
    assert "<link" not in html
    assert "<script" not in html
    assert "Run fingerprint" in html
    assert str(run_workdir) not in report_text
    assert str(run_workdir) not in html
    assert report_text.endswith("\n")
    assert markdown.endswith("\n")
    assert html.endswith("\n")

def test_json_is_key_sorted_for_stable_diffs(run_workdir):
    text = (run_workdir / config.REPORT_JSON).read_text(encoding="utf-8")
    report = json.loads(text)
    assert list(report) == sorted(report)
    assert text.startswith("{\n  \"attempts\"")
