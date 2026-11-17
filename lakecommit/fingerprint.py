"""Canonical JSON and stable content fingerprints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def canonical_json(value: object) -> str:
    """Serialize with sorted keys and no insignificant whitespace."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def pretty_json(value: object) -> str:
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


def object_fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_text_atomic(path: Path, text: str) -> None:
    """Write a complete text file, then atomically replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        try:
            import os

            os.fsync(handle.fileno())
        except OSError:
            pass
    temporary.replace(path)


def write_json_atomic(path: Path, value: object) -> None:
    write_text_atomic(path, pretty_json(value))
