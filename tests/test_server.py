import pytest

from lakecommit.errors import InputError
from lakecommit.server import create_app, validate_bind_host, validate_port


def test_read_only_app_serves_report_and_json_views(run_workdir):
    app = create_app(run_workdir)
    client = app.test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert b"Lake table commit verification bench" in page.data
    assert b"http://" not in page.data and b"https://" not in page.data
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in page.headers["Content-Security-Policy"]
    result = client.get("/api/result")
    assert result.status_code == 200
    assert result.get_json()["scenario"]["id"] == "add_files_accepted"
    assert client.get("/api/attempts").get_json()[0]["attempt_id"] == "a-alpha-001"
    assert client.get("/api/snapshots").get_json()[0]["sequence"] == 0
    assert client.get("/api/snapshots").get_json()[-1]["sequence"] == 1
    assert client.get("/api/attempt/a-alpha-001").status_code == 200
    assert client.get("/api/attempt/missing").status_code == 404
    assert client.get("/api/verify").get_json()["ok"] is True
    assert client.get("/healthz").get_json()["mode"] == "read-only"
    assert client.head("/api/result").status_code == 200


def test_write_methods_are_rejected(run_workdir):
    client = create_app(run_workdir).test_client()
    response = client.post("/api/result", json={"attempt": "x"})
    assert response.status_code == 405
    assert response.get_json()["error"]["code"] == "READ_ONLY"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_are_allowed(host):
    assert validate_bind_host(host) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "example.test", "::"])
def test_non_loopback_hosts_are_rejected(host):
    with pytest.raises(InputError) as caught:
        validate_bind_host(host)
    assert caught.value.code == "NON_LOOPBACK_HOST"
    assert caught.value.exit_code == 2


def test_ports_are_bounded():
    assert validate_port(8765) == 8765
    for value in (0, 65536, -1, True):
        with pytest.raises(InputError):
            validate_port(value)


def test_missing_report_is_an_input_error(tmp_path):
    with pytest.raises(InputError) as caught:
        create_app(tmp_path)
    assert caught.value.code == "REPORT_NOT_FOUND"
