"""SQLite audit ledger and persistent idempotency cache."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from lakecommit.errors import StateError
from lakecommit.fingerprint import canonical_json
from lakecommit.models import Decision, Snapshot


SCHEMA = """
CREATE TABLE IF NOT EXISTS attempt_audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL UNIQUE,
  writer_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  base_snapshot_id TEXT NOT NULL,
  logical_time TEXT NOT NULL,
  operation_type TEXT NOT NULL,
  operation_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  outcome TEXT NOT NULL,
  conflict_codes TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  resulting_snapshot_id TEXT,
  retry_required INTEGER NOT NULL,
  replay_of_attempt_id TEXT,
  replay_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS idempotency_cache (
  writer_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  first_attempt_id TEXT NOT NULL,
  first_status TEXT NOT NULL,
  first_outcome TEXT NOT NULL,
  first_snapshot_id TEXT,
  first_conflict_codes TEXT NOT NULL,
  first_decision_json TEXT NOT NULL,
  replay_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (writer_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS snapshot_audit (
  sequence INTEGER PRIMARY KEY,
  snapshot_id TEXT NOT NULL UNIQUE,
  parent_snapshot_id TEXT,
  logical_time TEXT NOT NULL,
  file_count INTEGER NOT NULL,
  attempt_id TEXT,
  operation_type TEXT NOT NULL,
  manifest_hash TEXT NOT NULL
);
"""


class Ledger:
    def __init__(self, path: str | Path, *, reset: bool = False, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        if reset and not read_only:
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(self.path) + suffix)
                if candidate.exists():
                    candidate.unlink()
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True)
        else:
            self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        if not read_only:
            self.connection.executescript(SCHEMA)
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def get_idempotency(self, writer_id: str, key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM idempotency_cache WHERE writer_id=? AND idempotency_key=?",
            (writer_id, key),
        ).fetchone()
        return dict(row) if row else None

    def _insert_attempt(
        self,
        decision: Decision,
        payload_hash: str,
        *,
        replay_of_attempt_id: str | None = None,
        replay_count: int = 0,
    ) -> None:
        attempt = decision.attempt
        try:
            self.connection.execute(
                """
                INSERT INTO attempt_audit (
                  attempt_id, writer_id, idempotency_key, base_snapshot_id,
                  logical_time, operation_type, operation_json, payload_hash,
                  status, outcome, conflict_codes, reasons_json,
                  resulting_snapshot_id, retry_required,
                  replay_of_attempt_id, replay_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.attempt_id,
                    attempt.writer_id,
                    attempt.idempotency_key,
                    attempt.base_snapshot_id,
                    attempt.logical_time,
                    attempt.operation_type,
                    canonical_json(attempt.operation),
                    payload_hash,
                    decision.status,
                    decision.outcome,
                    canonical_json(decision.conflict_codes),
                    canonical_json(decision.reasons),
                    decision.resulting_snapshot_id,
                    int(decision.retry_required),
                    replay_of_attempt_id,
                    replay_count,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise StateError(
                "ATTEMPT_ID_DUPLICATE",
                "attempt id already exists in the audit ledger",
                details={"attempt_id": attempt.attempt_id},
            ) from exc

    def record_attempt(self, decision: Decision, payload_hash: str) -> None:
        with self.connection:
            self._insert_attempt(decision, payload_hash)

    def record_first(self, decision: Decision, payload_hash: str) -> None:
        attempt = decision.attempt
        first_json = canonical_json(
            {
                **decision.core_dict(),
                "replay": None,
            }
        )
        with self.connection:
            self._insert_attempt(decision, payload_hash)
            try:
                self.connection.execute(
                    """
                    INSERT INTO idempotency_cache (
                      writer_id, idempotency_key, payload_hash, first_attempt_id,
                      first_status, first_outcome, first_snapshot_id,
                      first_conflict_codes, first_decision_json, replay_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        attempt.writer_id,
                        attempt.idempotency_key,
                        payload_hash,
                        attempt.attempt_id,
                        decision.status,
                        decision.outcome,
                        decision.resulting_snapshot_id,
                        canonical_json(decision.conflict_codes),
                        first_json,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StateError(
                    "IDEMPOTENCY_KEY_DUPLICATE",
                    "idempotency key was inserted concurrently",
                    details={"writer_id": attempt.writer_id, "idempotency_key": attempt.idempotency_key},
                ) from exc

    def claim_replay(self, writer_id: str, key: str) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE idempotency_cache
                SET replay_count = replay_count + 1
                WHERE writer_id=? AND idempotency_key=?
                """,
                (writer_id, key),
            )
            if cursor.rowcount != 1:
                raise StateError("IDEMPOTENCY_KEY_MISSING", "idempotency key disappeared before replay")
            row = self.connection.execute(
                "SELECT replay_count FROM idempotency_cache WHERE writer_id=? AND idempotency_key=?",
                (writer_id, key),
            ).fetchone()
            return int(row["replay_count"])

    def record_replay(self, decision: Decision, payload_hash: str) -> None:
        replay = decision.replay or {}
        self.record_attempt(decision, payload_hash)
        with self.connection:
            self.connection.execute(
                """
                UPDATE attempt_audit
                SET replay_of_attempt_id=?, replay_count=?
                WHERE attempt_id=?
                """,
                (
                    replay.get("replay_of_attempt_id"),
                    int(replay.get("replay_count", 0)),
                    decision.attempt.attempt_id,
                ),
            )

    def audit_snapshot(self, snapshot: Snapshot, commit: dict) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO snapshot_audit (
                  sequence, snapshot_id, parent_snapshot_id, logical_time,
                  file_count, attempt_id, operation_type, manifest_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.sequence,
                    snapshot.snapshot_id,
                    snapshot.parent_snapshot_id,
                    snapshot.logical_time,
                    len(snapshot.files),
                    commit.get("attempt_id"),
                    commit.get("operation_type", "bootstrap"),
                    snapshot.manifest_hash,
                ),
            )

    def _decode_attempt(self, row: sqlite3.Row) -> dict:
        value = dict(row)
        value["operation"] = json.loads(value.pop("operation_json"))
        value["conflict_codes"] = json.loads(value["conflict_codes"])
        value["reasons"] = json.loads(value.pop("reasons_json"))
        value["retry_required"] = bool(value["retry_required"])
        return value

    def list_attempts(self) -> list[dict]:
        rows = self.connection.execute("SELECT * FROM attempt_audit ORDER BY seq").fetchall()
        return [self._decode_attempt(row) for row in rows]

    def get_attempt(self, attempt_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM attempt_audit WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        return self._decode_attempt(row) if row else None

    def attempt_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM attempt_audit").fetchone()[0])

    def idempotency_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM idempotency_cache").fetchone()[0])
