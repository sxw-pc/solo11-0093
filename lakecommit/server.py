"""Read-only local review console. It never binds a non-loopback address."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, Response, jsonify, request

from lakecommit import config, report_html, report_json
from lakecommit.errors import InputError


def validate_bind_host(host: str) -> str:
    if host not in config.ALLOWED_BIND_HOSTS:
        raise InputError(
            "NON_LOOPBACK_HOST",
            "the review console may only bind to 127.0.0.1, localhost or ::1",
            details={"host": host, "allowed": sorted(config.ALLOWED_BIND_HOSTS)},
        )
    return host


def validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise InputError("PORT_INVALID", "port must be an integer between 1 and 65535")
    return port


def _read_report(workdir: str | Path) -> dict:
    path = Path(workdir) / config.REPORT_JSON
    if not path.is_file():
        raise InputError(
            "REPORT_NOT_FOUND",
            "run a scenario before starting the read-only console",
            location=config.REPORT_JSON,
        )
    try:
        return report_json.load_report(workdir)
    except (OSError, ValueError) as exc:
        raise InputError("REPORT_READ_ERROR", "report.json is not readable") from exc


def create_app(workdir: str | Path) -> Flask:
    report = _read_report(workdir)
    app = Flask(__name__)
    app.json.sort_keys = True
    app.json.ensure_ascii = False

    @app.before_request
    def read_only_guard():
        if request.method not in ("GET", "HEAD"):
            return jsonify({"error": {"code": "READ_ONLY", "message": "this console is read-only"}}), 405
        return None

    @app.after_request
    def security_headers(response: Response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    @app.get("/report")
    def report_page():
        html = report_html.render_html(report)
        banner = (
            '<div style="max-width:1180px;margin:20px auto 0;padding:10px 16px;'
            'border:1px solid #dde4eb;border-radius:12px;font:13px ui-sans-serif,system-ui">'
            "Read-only local console · "
            '<a href="/api/result">result JSON</a> · '
            '<a href="/api/attempts">attempts</a> · '
            '<a href="/api/snapshots">snapshots</a>'
            "</div>"
        )
        return Response(html.replace("<main>", "<main>" + banner, 1), mimetype="text/html")

    @app.get("/api/result")
    def api_result():
        return jsonify(report)

    @app.get("/api/attempts")
    def api_attempts():
        return jsonify(report["attempts"])

    @app.get("/api/snapshots")
    def api_snapshots():
        return jsonify(report["snapshot_chain"])

    @app.get("/api/verify")
    def api_verify():
        return jsonify(report["state_verification"])

    @app.get("/api/attempt/<attempt_id>")
    def api_attempt(attempt_id: str):
        for attempt in report["attempts"]:
            if attempt["attempt_id"] == attempt_id:
                return jsonify(attempt)
        return jsonify({"error": {"code": "ATTEMPT_NOT_FOUND", "message": "attempt is not in this report"}}), 404

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "mode": "read-only", "scenario_id": report["scenario"]["id"]})

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": {"code": "NOT_FOUND", "message": "route not found"}}), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return jsonify({"error": {"code": "READ_ONLY", "message": "this console is read-only"}}), 405

    return app


def serve(workdir: str | Path, host: str = config.DEFAULT_HOST, port: int = config.DEFAULT_PORT) -> None:
    validate_bind_host(host)
    validate_port(port)
    app = create_app(workdir)
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
