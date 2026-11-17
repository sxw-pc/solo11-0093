"""Deterministic Markdown rendering of a normalized run report."""

from __future__ import annotations

from pathlib import Path

from lakecommit import config
from lakecommit.fingerprint import canonical_json, write_text_atomic


def _text(value: object) -> str:
    if value is None:
        return "-"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _details(value: object) -> str:
    return canonical_json(value) if value else ""


def _reason_lines(reasons: list[dict]) -> list[str]:
    lines: list[str] = []
    for item in reasons:
        suffix = f" — `{_details(item.get('details'))}`" if item.get("details") else ""
        lines.append(f"  - `{item['code']}` [{item['severity']}] {item['message']}{suffix}")
    return lines


def render_markdown(report: dict) -> str:
    scenario = report["scenario"]
    table = report["table"]
    summary = report["summary"]
    lines = [
        f"# Lake commit verification: {scenario['name']}",
        "",
        f"- Scenario: `{scenario['id']}`",
        f"- Table: `{table['table_name']}`",
        f"- Partition columns: `{', '.join(table['partition_columns'])}`",
        f"- Initial snapshot: `{report['initial_snapshot']['snapshot_id']}`",
        f"- Final snapshot: `{report['final_snapshot']['snapshot_id']}`",
        f"- Run fingerprint: `{report['run_fingerprint']}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key in (
        "attempts",
        "accepted",
        "rejected",
        "applied",
        "replayed",
        "retry_required",
        "safe_rebases",
        "snapshots_created",
        "final_file_count",
    ):
        lines.append(f"| {key} | {summary[key]} |")
    lines.extend(["", "### Conflict codes", ""])
    if summary["conflict_code_counts"]:
        lines.extend(["| Code | Count |", "| --- | ---: |"])
        for code, count in summary["conflict_code_counts"].items():
            lines.append(f"| `{code}` | {count} |")
    else:
        lines.append("No conflicts.")
    lines.extend(
        [
            "",
            "## Attempt decisions",
            "",
            "| # | Attempt | Writer | Base | Operation | Status | Outcome | Retry | Conflicts |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for index, attempt in enumerate(report["attempts"], start=1):
        codes = ", ".join(f"`{code}`" for code in attempt["conflict_codes"]) or "-"
        lines.append(
            "| {index} | `{attempt_id}` | `{writer}` | `{base}` | `{operation}` | {status} | {outcome} | {retry} | {codes} |".format(
                index=index,
                attempt_id=attempt["attempt_id"],
                writer=attempt["writer_id"],
                base=attempt["base_snapshot_id"],
                operation=attempt["operation_type"],
                status=attempt["status"],
                outcome=attempt["outcome"],
                retry="yes" if attempt["retry_required"] else "no",
                codes=codes,
            )
        )
    lines.extend(["", "### Decision details", ""])
    for index, attempt in enumerate(report["attempts"], start=1):
        lines.append(f"#### {index}. `{attempt['attempt_id']}`")
        lines.append("")
        lines.extend(_reason_lines(attempt["reasons"]))
        if attempt.get("replay"):
            lines.append(f"  - Replay source: `{attempt['replay']['replay_of_attempt_id']}`; ordinal {attempt['replay']['replay_count']}")
        if attempt.get("rebase"):
            lines.append(f"  - Rebase evidence: `{canonical_json(attempt['rebase'])}`")
        if attempt.get("resulting_snapshot_id"):
            lines.append(f"  - Resulting snapshot: `{attempt['resulting_snapshot_id']}`")
        lines.append("")
    lines.extend(
        [
            "## Snapshot chain",
            "",
            "| Sequence | Snapshot | Parent | Attempt | Operation | Files | Logical time |",
            "| ---: | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for item in report["snapshot_chain"]:
        lines.append(
            f"| {item['sequence']} | `{item['snapshot_id']}` | {_text(item['parent_snapshot_id'])} | "
            f"{_text(item['attempt_id'])} | `{item['operation_type']}` | {item['file_count']} | {item['logical_time']} |"
        )
    lines.extend(
        [
            "",
            "## Final files",
            "",
            "| Path | Partition | Bytes | Rows | Content hash |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    for entry in report["final_snapshot"]["files"]:
        lines.append(
            f"| `{entry['path']}` | `{canonical_json(entry['partition'])}` | {entry['size_bytes']} | "
            f"{entry['row_count']} | `{entry['content_hash']}` |"
        )
    verification = report["state_verification"]
    lines.extend(
        [
            "",
            "## State verification",
            "",
            f"- Result: **{'ok' if verification['ok'] else 'failed'}**",
            f"- Current snapshot: `{_text(verification['current_snapshot_id'])}`",
            f"- Snapshots checked: {verification['snapshot_count']}",
            "",
        ]
    )
    for check in verification["checks"]:
        lines.append(f"- {'PASS' if check['ok'] else 'FAIL'} `{check['name']}`")
    if verification["errors"]:
        lines.extend(["", "Errors:"])
        for error in verification["errors"]:
            lines.append(f"- `{error['code']}` {error['message']}")
    lines.extend(
        [
            "",
            "---",
            "",
            "This report is deterministic, synthetic, and generated without wall-clock time.",
            "",
        ]
    )
    return "\n".join(lines)


def write_markdown_report(workdir: str | Path, report: dict) -> Path:
    path = Path(workdir) / config.REPORT_MD
    write_text_atomic(path, render_markdown(report))
    return path
