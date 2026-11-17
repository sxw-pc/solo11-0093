import socket

import pytest

from lakecommit import config, runner
from lakecommit.errors import NetworkBlockedError
from tools.network_guard import blocked_network, scan_offline


def test_guard_blocks_outbound_sockets():
    with blocked_network():
        with pytest.raises(NetworkBlockedError):
            socket.create_connection(("example.test", 443), timeout=0.1)
        with pytest.raises(NetworkBlockedError):
            socket.getaddrinfo("example.test", 443)


def test_demo_path_runs_without_network_and_scanner_is_clean(tmp_path):
    with blocked_network():
        report = runner.run_scenario(
            config.SCENARIOS_DIR / "13_mixed_workload.yaml", tmp_path / "offline"
        )
    assert report["state_verification"]["ok"] is True
    result = scan_offline(config.ROOT)
    assert result["ok"] is True, result
    html = (tmp_path / "offline" / config.REPORT_HTML).read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html
