#!/usr/bin/env python
"""Offline guard and source scanner used by the test suite."""

from __future__ import annotations

import ast
import re
import socket
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lakecommit.errors import NetworkBlockedError  # noqa: E402

FORBIDDEN_IMPORTS = {
    "aiohttp",
    "httpx",
    "requests",
    "urllib3",
    "websocket",
    "websockets",
}
URL_RE = re.compile("https?" + "://")
SKIP_PARTS = {"__pycache__", ".git", ".pytest_cache", ".lakecommit-demo"}


def _is_loopback(address: object) -> bool:
    if isinstance(address, tuple) and address:
        return str(address[0]) in {"127.0.0.1", "::1", "localhost"}
    if isinstance(address, str):
        return address in {"127.0.0.1", "::1", "localhost"}
    return False


@contextmanager
def blocked_network(allow_loopback: bool = False):
    """Block outbound socket operations for the duration of the context."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo

    def guard_connect(self, address):
        if allow_loopback and _is_loopback(address):
            return original_connect(self, address)
        raise NetworkBlockedError("outbound network access is disabled")

    def guard_connect_ex(self, address):
        if allow_loopback and _is_loopback(address):
            return original_connect_ex(self, address)
        raise NetworkBlockedError("outbound network access is disabled")

    def guard_create_connection(address, *args, **kwargs):
        if allow_loopback and _is_loopback(address):
            return original_create_connection(address, *args, **kwargs)
        raise NetworkBlockedError("outbound network access is disabled")

    def guard_getaddrinfo(host, *args, **kwargs):
        if allow_loopback and _is_loopback(host):
            return original_getaddrinfo(host, *args, **kwargs)
        raise NetworkBlockedError("DNS access is disabled")

    socket.socket.connect = guard_connect
    socket.socket.connect_ex = guard_connect_ex
    socket.create_connection = guard_create_connection
    socket.getaddrinfo = guard_getaddrinfo
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.create_connection = original_create_connection
        socket.getaddrinfo = original_getaddrinfo


def _python_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if any(part in SKIP_PARTS or part.startswith(".lakecommit") for part in relative.parts):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def scan_offline(root: str | Path = ROOT, include_tools: bool = True) -> dict:
    root_path = Path(root).resolve()
    search = [root_path / "lakecommit"]
    if include_tools:
        search.append(root_path / "tools")
    forbidden: list[dict] = []
    urls: list[dict] = []
    for directory in search:
        if not directory.exists():
            continue
        for path in _python_files(directory):
            relative = path.relative_to(root_path).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(text, filename=relative)
            except SyntaxError:
                forbidden.append({"file": relative, "module": "<syntax-error>"})
                continue
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.split(".")[0] in FORBIDDEN_IMPORTS:
                        forbidden.append({"file": relative, "module": name})
            if URL_RE.search(text):
                urls.append({"file": relative, "pattern": "external-url-literal"})
    return {
        "ok": not forbidden and not urls,
        "forbidden_imports": forbidden,
        "external_urls": urls,
        "scanned_root": ".",
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] != "scan":
        print("usage: network_guard.py scan [root]")
        return 2
    root = Path(args[1]) if len(args) > 1 else ROOT
    result = scan_offline(root)
    print("offline scan:", "OK" if result["ok"] else "VIOLATED")
    for item in result["forbidden_imports"]:
        print("forbidden import", item)
    for item in result["external_urls"]:
        print("external URL", item)
    return 0 if result["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
