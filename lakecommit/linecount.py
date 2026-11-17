"""Project code-size accounting and budget checks."""

from __future__ import annotations

from pathlib import Path

MIN_LINES = 2500
MAX_LINES = 4500
MAX_FILE_LINES = 300
EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
    ".lakecommit-demo",
}


def iter_code_files(root: str | Path) -> list[Path]:
    root_path = Path(root).resolve()
    files: list[Path] = []
    for path in root_path.rglob("*.py"):
        relative = path.relative_to(root_path)
        if any(part in EXCLUDED_DIRS or part.startswith(".lakecommit") for part in relative.parts):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root_path).as_posix())


def count_file(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for _line in handle)


def collect(root: str | Path) -> dict:
    root_path = Path(root).resolve()
    files = []
    for path in iter_code_files(root_path):
        files.append(
            {
                "path": path.relative_to(root_path).as_posix(),
                "lines": count_file(path),
            }
        )
    total = sum(item["lines"] for item in files)
    oversized = [item for item in files if item["lines"] > MAX_FILE_LINES]
    budget_ok = MIN_LINES <= total <= MAX_LINES and not oversized
    return {
        "root": ".",
        "file_count": len(files),
        "total_lines": total,
        "min_lines": MIN_LINES,
        "max_lines": MAX_LINES,
        "max_file_lines": MAX_FILE_LINES,
        "oversized_files": oversized,
        "budget_ok": budget_ok,
        "files": files,
    }


def format_report(report: dict) -> str:
    lines = [
        f"python files: {report['file_count']}",
        f"total lines: {report['total_lines']} (budget {report['min_lines']}-{report['max_lines']})",
        f"largest file limit: {report['max_file_lines']}",
    ]
    if report["oversized_files"]:
        for item in report["oversized_files"]:
            lines.append(f"OVER BUDGET: {item['path']} has {item['lines']} lines")
    lines.append("budget: " + ("OK" if report["budget_ok"] else "VIOLATED"))
    return "\n".join(lines)
