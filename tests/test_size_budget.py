from lakecommit import config, linecount

def test_code_size_budget_and_per_file_limit():
    report = linecount.collect(config.ROOT)
    assert report["budget_ok"] is True
    assert linecount.MIN_LINES <= report["total_lines"] <= linecount.MAX_LINES
    assert report["oversized_files"] == []
    assert report["total_lines"] >= 2500
    assert all(item["lines"] <= 300 for item in report["files"])

def test_required_modules_scenarios_and_tests_exist():
    paths = {item["path"] for item in linecount.collect(config.ROOT)["files"]}
    required = {
        "lakecommit/__main__.py",
        "lakecommit/cli.py",
        "lakecommit/protocol.py",
        "lakecommit/report_json.py",
        "lakecommit/report_md.py",
        "lakecommit/report_html.py",
        "lakecommit/server.py",
        "tests/test_golden.py",
        "tests/test_determinism.py",
        "tests/test_offline_guard.py",
        "tests/test_size_budget.py",
        "tools/linecount.py",
        "tools/network_guard.py",
    }
    assert required <= paths
    assert len(list(config.SCENARIOS_DIR.glob("*.yaml"))) >= 12
    assert len(list(config.EXPECTED_DIR.glob("*.json"))) >= 12
