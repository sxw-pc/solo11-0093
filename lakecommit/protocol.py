"""Optimistic file-level commit protocol orchestration."""

from __future__ import annotations

from lakecommit import conflicts, idempotency, validator
from lakecommit.conflicts import Code, reason
from lakecommit.errors import SchemaError
from lakecommit.fingerprint import object_fingerprint
from lakecommit.ledger import Ledger
from lakecommit.models import Attempt, Decision, TableSpec
from lakecommit.operations import evaluate_operation
from lakecommit.store import SnapshotStore


class ProtocolEngine:
    def __init__(
        self,
        table: TableSpec,
        store: SnapshotStore,
        ledger: Ledger,
        protocol: dict | None = None,
    ) -> None:
        self.table = table
        self.store = store
        self.ledger = ledger
        self.protocol = protocol or {"allow_safe_rebase": True}
        self.allow_safe_rebase = bool(self.protocol.get("allow_safe_rebase", True))

    def submit_document(self, document: object) -> Decision:
        try:
            attempt = validator.validate_attempt_document(
                document,
                location="submission",
                columns=self.table.partition_columns,
            )
        except SchemaError as exc:
            if not isinstance(document, dict) or not isinstance(document.get("attempt_id"), str):
                raise
            attempt = Attempt(
                attempt_id=document["attempt_id"] or "invalid-attempt",
                writer_id=str(document.get("writer_id", "unknown-writer")),
                idempotency_key=str(document.get("idempotency_key", "unknown-key")),
                base_snapshot_id=str(document.get("base_snapshot_id", "unknown-base")),
                logical_time=str(document.get("logical_time", "1970-01-01T00:00:00Z")),
                operation={"type": "invalid", "files": []},
            )
            return self._validation_rejection(attempt, exc, object_fingerprint(document))
        return self.submit(attempt)

    def _validation_rejection(
        self,
        attempt: Attempt,
        error: SchemaError,
        digest: str,
    ) -> Decision:
        decision = Decision.rejected(
            attempt,
            digest,
            [
                reason(
                    Code.VALIDATION_ERROR,
                    error.message,
                    details={
                        "validation_code": error.code,
                        "field_path": error.field_path,
                    },
                )
            ],
            [Code.VALIDATION_ERROR],
        )
        self.ledger.record_attempt(decision, digest)
        return decision

    def submit(self, attempt: Attempt) -> Decision:
        try:
            attempt = validator.validate_attempt_document(
                attempt.to_dict(),
                location=f"attempt:{attempt.attempt_id}",
                columns=self.table.partition_columns,
            )
        except SchemaError as exc:
            return self._validation_rejection(
                attempt, exc, object_fingerprint(attempt.to_dict())
            )

        digest = idempotency.payload_hash(attempt)
        existing = self.ledger.get_idempotency(attempt.writer_id, attempt.idempotency_key)
        if existing:
            if existing["payload_hash"] != digest:
                decision = idempotency.mismatch_decision(
                    attempt, digest, str(existing["first_attempt_id"])
                )
                self.ledger.record_attempt(decision, digest)
                return decision
            replay_count = self.ledger.claim_replay(
                attempt.writer_id, attempt.idempotency_key
            )
            decision = idempotency.replay_decision(
                existing, attempt, digest, replay_count
            )
            self.ledger.record_replay(decision, digest)
            return decision

        current = self.store.current_snapshot()
        if not self.store.has_snapshot(attempt.base_snapshot_id):
            decision = self._reject_base(
                attempt,
                digest,
                Code.BASE_SNAPSHOT_UNKNOWN,
                "base snapshot does not exist in this snapshot chain",
                {"base_snapshot_id": attempt.base_snapshot_id},
            )
            self._record_first(decision, digest)
            return decision
        if not self.store.is_ancestor(attempt.base_snapshot_id, current.snapshot_id):
            decision = self._reject_base(
                attempt,
                digest,
                Code.BASE_SNAPSHOT_NOT_ANCESTOR,
                "base snapshot is not an ancestor of the current snapshot",
                {
                    "base_snapshot_id": attempt.base_snapshot_id,
                    "current_snapshot_id": current.snapshot_id,
                },
            )
            self._record_first(decision, digest)
            return decision

        base = self.store.load_snapshot(attempt.base_snapshot_id)
        changes = (
            []
            if base.snapshot_id == current.snapshot_id
            else self.store.changes_since(base.snapshot_id, current.snapshot_id)
        )
        result = evaluate_operation(
            attempt,
            base,
            current,
            changes,
            self.table.partition_columns,
            self.allow_safe_rebase,
        )
        if not result.accepted:
            decision = Decision.rejected(
                attempt,
                digest,
                result.reasons,
                result.conflict_codes,
                retry_required=conflicts.needs_rebase(result.conflict_codes),
                rebase=result.rebase,
            )
            self._record_first(decision, digest)
            return decision

        snapshot, commit = self.store.commit_snapshot(
            files=result.files,
            attempt=attempt,
            payload_hash=digest,
            base_snapshot_id=attempt.base_snapshot_id,
            touched_partitions=result.touched_partitions,
            added_paths=result.added_paths,
            removed_paths=result.removed_paths,
            rebase=result.rebase,
        )
        self.ledger.audit_snapshot(snapshot, commit)
        decision = Decision.accepted(
            attempt,
            digest,
            result.reasons,
            snapshot.snapshot_id,
            rebase=result.rebase,
        )
        self._record_first(decision, digest)
        return decision

    def _reject_base(
        self,
        attempt: Attempt,
        digest: str,
        code: str,
        message: str,
        details: dict,
    ) -> Decision:
        return Decision.rejected(
            attempt,
            digest,
            [reason(code, message, details=details)],
            [code],
            retry_required=True,
        )

    def _record_first(self, decision: Decision, digest: str) -> None:
        self.ledger.record_first(decision, digest)
