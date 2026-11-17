"""Self-contained HTML report with inline CSS and no external resources."""

from __future__ import annotations

from html import escape
from pathlib import Path

from lakecommit import config
from lakecommit.fingerprint import canonical_json, write_text_atomic


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _json(value: object) -> str:
    return _e(canonical_json(value))


def _reason_html(reasons: list[dict]) -> str:
    rows = []
    for item in reasons:
        details = ""
        if item.get("details"):
            details = f'<code class="details">{_json(item["details"])}</code>'
        rows.append(
            f'<li><span class="code">{_e(item["code"])}</span>'
            f'<span class="sev { _e(item["severity"]) }">{_e(item["severity"])}</span>'
            f'<span>{_e(item["message"])}</span>{details}</li>'
        )
    return "".join(rows)


def render_html(report: dict) -> str:
    scenario = report["scenario"]
    summary = report["summary"]
    verification = report["state_verification"]
    attempts = report["attempts"]
    cards = [
        ("attempts", summary["attempts"]),
        ("accepted", summary["accepted"]),
        ("rejected", summary["rejected"]),
        ("replayed", summary["replayed"]),
        ("safe rebases", summary["safe_rebases"]),
        ("snapshots", summary["snapshots_created"]),
    ]
    card_html = "".join(
        f'<div class="stat"><span>{_e(label)}</span><strong>{_e(value)}</strong></div>'
        for label, value in cards
    )
    attempt_html = []
    for index, attempt in enumerate(attempts, start=1):
        state = "accepted" if attempt["status"] == "accepted" else "rejected"
        replay = ""
        if attempt.get("replay"):
            replay = (
                f'<p class="note">Replays <code>{_e(attempt["replay"]["replay_of_attempt_id"])}</code>'
                f' (ordinal {_e(attempt["replay"]["replay_count"])})</p>'
            )
        rebase = ""
        if attempt.get("rebase"):
            rebase = f'<pre class="evidence">{_json(attempt["rebase"])}</pre>'
        codes = "".join(f'<span class="badge">{_e(code)}</span>' for code in attempt["conflict_codes"])
        attempt_html.append(
            f'<article class="attempt {state}">'
            f'<header><div><span class="ordinal">{index:02d}</span> <code>{_e(attempt["attempt_id"])}</code></div>'
            f'<span class="status">{_e(attempt["status"])}</span></header>'
            f'<div class="meta"><span>writer <code>{_e(attempt["writer_id"])}</code></span>'
            f'<span>base <code>{_e(attempt["base_snapshot_id"])}</code></span>'
            f'<span>op <code>{_e(attempt["operation_type"])}</code></span>'
            f'<span>time <code>{_e(attempt["logical_time"])}</code></span></div>'
            f'<div class="badges">{codes}</div>'
            f'{replay}{rebase}'
            f'<details><summary>Decision reasons and payload</summary><ul class="reasons">'
            f'{_reason_html(attempt["reasons"])}</ul><pre class="evidence">{_json(attempt["operation"])}</pre></details>'
            f'</article>'
        )
    chain_rows = "".join(
        f'<tr><td>{_e(item["sequence"])}</td><td><code>{_e(item["snapshot_id"])}</code></td>'
        f'<td>{_e(item["parent_snapshot_id"] or "-")}</td><td>{_e(item["attempt_id"] or "-")}</td>'
        f'<td><code>{_e(item["operation_type"])}</code></td><td>{_e(item["file_count"])}</td>'
        f'<td><code>{_e(item["logical_time"])}</code></td></tr>'
        for item in report["snapshot_chain"]
    )
    file_rows = "".join(
        f'<tr><td><code>{_e(entry["path"])}</code></td><td><code>{_json(entry["partition"])}</code></td>'
        f'<td>{_e(entry["size_bytes"])}</td><td>{_e(entry["row_count"])}</td>'
        f'<td><code>{_e(entry["content_hash"])}</code></td></tr>'
        for entry in report["final_snapshot"]["files"]
    )
    conflict_html = (
        "".join(
            f'<span class="badge">{_e(code)} × {_e(count)}</span>'
            for code, count in summary["conflict_code_counts"].items()
        )
        or '<span class="muted">No conflicts recorded.</span>'
    )
    checks_html = "".join(
        f'<li class="{"pass" if check["ok"] else "fail"}">{_e(check["name"])}</li>'
        for check in verification["checks"]
    )
    errors_html = "".join(
        f'<li><code>{_e(error["code"])}</code> {_e(error["message"])}</li>'
        for error in verification["errors"]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lake commit verification · {_e(scenario["id"])}</title>
<style>
:root {{ color-scheme: light dark; --ink:#18212b; --muted:#617080; --paper:#f5f7f9; --panel:#ffffff;
  --line:#dde4eb; --accent:#0b6e69; --good:#176b45; --bad:#a33a2b; --goodbg:#e9f5ef; --badbg:#fbece9; }}
@media (prefers-color-scheme: dark) {{ :root {{ --ink:#e9eef3; --muted:#9cabb9; --paper:#10161c;
  --panel:#18212a; --line:#2b3945; --accent:#66c7bf; --good:#82d2a8; --bad:#ff9d8a;
  --goodbg:#173128; --badbg:#3a211d; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--paper); color:var(--ink); font:15px/1.55 ui-sans-serif,system-ui,sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:44px 24px 72px; }}
h1 {{ margin:0 0 8px; font-size:clamp(28px,4vw,48px); letter-spacing:-.035em; line-height:1.05; }}
h2 {{ margin:38px 0 14px; font-size:21px; letter-spacing:-.01em; }}
h3 {{ margin:26px 0 10px; font-size:16px; }}
p {{ margin:8px 0; }}
code, pre {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }}
code {{ font-size:.9em; overflow-wrap:anywhere; }}
.eyebrow {{ color:var(--accent); text-transform:uppercase; letter-spacing:.16em; font-size:12px; font-weight:700; }}
.sub {{ color:var(--muted); max-width:760px; }}
.stats {{ display:grid; grid-template-columns:repeat(6,1fr); gap:10px; margin:26px 0; }}
.stat {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:15px; }}
.stat span {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
.stat strong {{ display:block; font-size:28px; margin-top:4px; }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:20px; overflow:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.07em; }}
.attempt {{ background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--muted);
  border-radius:14px; padding:16px 18px; margin:12px 0; }}
.attempt.accepted {{ border-left-color:var(--good); }}
.attempt.rejected {{ border-left-color:var(--bad); }}
.attempt header {{ display:flex; justify-content:space-between; gap:12px; align-items:center; }}
.ordinal {{ color:var(--muted); font-size:12px; }}
.status {{ font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.08em; }}
.accepted .status {{ color:var(--good); }} .rejected .status {{ color:var(--bad); }}
.meta {{ display:flex; flex-wrap:wrap; gap:12px 20px; color:var(--muted); font-size:12px; margin:10px 0; }}
.badges {{ display:flex; flex-wrap:wrap; gap:6px; }}
.badge {{ display:inline-block; border:1px solid var(--line); border-radius:999px; padding:3px 9px;
  font:11px ui-monospace,monospace; color:var(--bad); background:var(--badbg); }}
.note {{ color:var(--muted); font-size:13px; }}
details {{ margin-top:12px; }} summary {{ cursor:pointer; color:var(--accent); font-size:13px; }}
.reasons {{ padding-left:20px; }} .reasons li {{ margin:8px 0; }}
.reasons .code {{ font-weight:700; margin-right:8px; }}
.sev {{ font-size:10px; text-transform:uppercase; margin-right:8px; color:var(--muted); }}
.sev.error {{ color:var(--bad); }} .details {{ display:block; color:var(--muted); margin-top:3px; }}
.evidence {{ background:var(--paper); border:1px solid var(--line); border-radius:10px; padding:10px;
  white-space:pre-wrap; font-size:12px; color:var(--muted); overflow:auto; }}
.checks {{ display:flex; flex-wrap:wrap; gap:8px; padding:0; list-style:none; }}
.checks li {{ border:1px solid var(--line); border-radius:999px; padding:5px 10px; font:12px ui-monospace,monospace; }}
.checks .pass {{ color:var(--good); background:var(--goodbg); }}
.checks .fail {{ color:var(--bad); background:var(--badbg); }}
.fingerprint {{ font-size:12px; color:var(--muted); overflow-wrap:anywhere; }}
.muted {{ color:var(--muted); }}
.split {{ display:grid; grid-template-columns:1.2fr .8fr; gap:14px; }}
@media (max-width:820px) {{ .stats {{ grid-template-columns:repeat(2,1fr); }} .split {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<main>
<div class="eyebrow">Lake table commit verification bench</div>
<h1>{_e(scenario["name"])}</h1>
<p class="sub">{_e(scenario.get("description", ""))}</p>
<div class="stats">{card_html}</div>
<div class="panel">
<table><tbody>
<tr><th>Scenario</th><td><code>{_e(scenario["id"])}</code></td><th>Table</th><td><code>{_e(report["table"]["table_name"])}</code></td></tr>
<tr><th>Initial snapshot</th><td><code>{_e(report["initial_snapshot"]["snapshot_id"])}</code></td><th>Final snapshot</th><td><code>{_e(report["final_snapshot"]["snapshot_id"])}</code></td></tr>
<tr><th>Run fingerprint</th><td colspan="3" class="fingerprint">{_e(report["run_fingerprint"])}</td></tr>
</tbody></table>
</div>
<h2>Conflict summary</h2>
<div class="badges">{conflict_html}</div>
<h2>Attempt decisions</h2>
{''.join(attempt_html)}
<h2>Snapshot chain</h2>
<div class="panel"><table><thead><tr><th>Seq</th><th>Snapshot</th><th>Parent</th><th>Attempt</th><th>Operation</th><th>Files</th><th>Logical time</th></tr></thead><tbody>{chain_rows}</tbody></table></div>
<h2>Final files</h2>
<div class="panel"><table><thead><tr><th>Path</th><th>Partition</th><th>Bytes</th><th>Rows</th><th>Content hash</th></tr></thead><tbody>{file_rows}</tbody></table></div>
<div class="split">
<section><h2>State verification</h2><div class="panel"><p><strong>{'PASS' if verification["ok"] else 'FAIL'}</strong> · {_e(verification["snapshot_count"])} snapshots checked</p><ul class="checks">{checks_html}</ul>{f'<ul>{errors_html}</ul>' if errors_html else ''}</div></section>
<section><h2>Protocol</h2><div class="panel"><pre class="evidence">{_json(report["protocol"])}</pre></div></section>
</div>
<p class="muted" style="margin-top:32px">Deterministic synthetic fixture report. No wall-clock time, random identifiers, external assets, or network requests are used.</p>
</main>
</body>
</html>
"""


def write_html_report(workdir: str | Path, report: dict) -> Path:
    path = Path(workdir) / config.REPORT_HTML
    write_text_atomic(path, render_html(report))
    return path
