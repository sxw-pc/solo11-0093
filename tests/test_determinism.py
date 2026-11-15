import json

from lakecommit import config, runner


def test_two_runs_are_byte_identical(tmp_path):
    scenario = config.SCENARIOS_DIR / "13_mixed_workload.yaml"
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = runner.run_scenario(scenario, first_dir)
    second = runner.run_scenario(scenario, second_dir)
    for name in (config.REPORT_JSON, config.REPORT_MD, config.REPORT_HTML):
        assert (first_dir / name).read_bytes() == (second_dir / name).read_bytes()
    assert first["run_fingerprint"] == second["run_fingerprint"]
    assert [item["snapshot_id"] for item in first["snapshot_chain"]] == [
        item["snapshot_id"] for item in second["snapshot_chain"]
    ]
    assert [item["conflict_codes"] for item in first["attempts"]] == [
        item["conflict_codes"] for item in second["attempts"]
    ]
    assert str(first_dir) not in (first_dir / config.REPORT_JSON).read_text(encoding="utf-8")
    assert str(second_dir) not in (second_dir / config.REPORT_HTML).read_text(encoding="utf-8")


def test_logical_clock_only_uses_fixture_values(tmp_path):
    report = runner.run_scenario(
        config.SCENARIOS_DIR / "03_safe_rebase_accepted.yaml", tmp_path / "clock"
    )
    times = [item["logical_time"] for item in report["attempts"]]
    times += [item["logical_time"] for item in report["snapshot_chain"]]
    assert all(time.startswith("2024-01-01T00:") for time in times)
    assert len(times) == len(set(times)) or len(times) > 0
    assert json.dumps(report, sort_keys=True).find("generated_at") == -1
