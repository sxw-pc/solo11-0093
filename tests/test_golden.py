import json

import pytest

from lakecommit import config, runner
from lakecommit.errors import GoldenMismatchError

def test_every_scenario_matches_its_committed_golden(tmp_path):
    scenarios = runner.list_scenarios()
    assert len(scenarios) >= 12
    scenario_ids = set()
    for scenario in scenarios:
        bundle_path = scenario
        workdir = tmp_path / scenario.stem
        report = runner.run_scenario(bundle_path, workdir)
        scenario_ids.add(report["scenario"]["id"])
        golden = runner.verify_golden(workdir)
        assert golden["match"] is True
        assert golden["scenario_id"] == report["scenario"]["id"]
        assert report["state_verification"]["ok"] is True
    expected_ids = {path.stem for path in config.EXPECTED_DIR.glob("*.json")}
    assert scenario_ids <= expected_ids

def test_golden_mismatch_has_exit_code_four(tmp_path):
    workdir = tmp_path / "tampered"
    runner.run_scenario(config.SCENARIOS_DIR / "01_add_files_accepted.yaml", workdir)
    report_path = workdir / config.REPORT_JSON
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["summary"]["attempts"] = 99
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(GoldenMismatchError) as caught:
        runner.verify_golden(workdir)
    assert caught.value.code == "GOLDEN_MISMATCH"
    assert caught.value.exit_code == 4
    assert "summary" in caught.value.details["differing_fields"]
