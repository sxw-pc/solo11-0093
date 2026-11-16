"""Ledger persistence for crash recovery conclusions."""

from __future__ import annotations

import json

from lakecommit.errors import StateError
from lakecommit.fingerprint import canonical_json


class RecoveryLedgerMixin:
    def record_recovery(
        self,
        *,
        attempt_id: str,
        route: str,
        resolution: str,
        payload_hash: str,
        snapshot_id: str | None,
        parent_snapshot_id: str | None,
        writer_id: str,
        idempotency_key: str,
        evidence: dict,
        reasons: list[dict],
    ) -> None:
        """Append a recovery conclusion; the journal file decides first-wins per incident."""
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO recovery_audit (
                  attempt_id, route, resolution, snapshot_id, parent_snapshot_id,
                  payload_hash, evidence_json, reasons_json, writer_id, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    route,
                    resolution,
                    snapshot_id,
                    parent_snapshot_id,
                    payload_hash,
                    canonical_json(evidence),
                    canonical_json(reasons),
                    writer_id,
                    idempotency_key,
                ),
            )

    def get_recovery(self, attempt_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM recovery_audit WHERE attempt_id=? ORDER BY id DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["evidence"] = json.loads(value.pop("evidence_json"))
        value["reasons"] = json.loads(value.pop("reasons_json"))
        return value

    def recovery_records(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM recovery_audit ORDER BY id"
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["evidence"] = json.loads(value.pop("evidence_json"))
            value["reasons"] = json.loads(value.pop("reasons_json"))
            result.append(value)
        return result

    def recovery_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM recovery_audit").fetchone()[0])

    def attempt_exists(self, attempt_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM attempt_audit WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        return row is not None

    def has_snapshot_audit(self, snapshot_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM snapshot_audit WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return row is not None

    def delete_snapshot_audit(self, snapshot_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM snapshot_audit WHERE snapshot_id=?", (snapshot_id,)
            )

    def delete_recovery_decision(self, attempt_id: str) -> None:
        """Remove a synthetic completion row if the attempt is rolled back instead."""
        with self.connection:
            self.connection.execute(
                "DELETE FROM attempt_audit WHERE attempt_id=? AND outcome='applied'", (attempt_id,)
            )

    def confirmed_snapshot(self, snapshot_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM idempotency_cache WHERE first_snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return row is not None

    def record_first_cache_only(self, decision, payload_hash: str) -> None:
        """Complete the idempotency first result after an attempt-audit-only crash."""
        attempt = decision.attempt
        first_json = canonical_json({**decision.core_dict(), "replay": None})
        with self.connection:
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
