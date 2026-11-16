"""Applying a recovery route to the snapshot store and audit ledger."""

from __future__ import annotations

from lakecommit import recovery_evidence as evidence_helpers
from lakecommit.errors import StateError
from lakecommit.models import Attempt


def materialize_completion(store, ledger, intent: dict, published: object | None, reasons: list[dict]) -> None:
    """Finish snapshot directory, current, audit and first result; each step idempotent."""
    attempt = Attempt.from_dict(intent["attempt"])
    snapshot_id = str(intent["snapshot_id"])
    parent_id = intent.get("parent_snapshot_id")
    if not store.has_snapshot(snapshot_id):
        store.publish_prepared(intent["manifest"], intent["commit"])
    snapshot = published or store.load_snapshot(snapshot_id)
    commit = store.load_commit(snapshot_id)
    if store.current_snapshot_id() == parent_id:
        store.publish_current(snapshot_id)
    ledger.audit_snapshot(snapshot, commit)
    if ledger.get_idempotency(attempt.writer_id, attempt.idempotency_key) is None:
        decision = evidence_helpers.accepted_decision(intent, reasons)
        existing = ledger.get_attempt(attempt.attempt_id)
        if existing is None:
            ledger.record_first(decision, str(intent["payload_hash"]))
        elif _matches(existing, attempt, intent):
            ledger.record_first_cache_only(decision, str(intent["payload_hash"]))
        else:
            raise StateError(
                "RECOVERY_EVIDENCE_CONFLICT",
                "attempt audit exists without a matching idempotency first result",
                details={"attempt_id": attempt.attempt_id},
            )


def rollback_publication(store, ledger, intent: dict) -> None:
    """Return current to the verified parent and delete the unconfirmed snapshot."""
    attempt = Attempt.from_dict(intent["attempt"])
    snapshot_id = str(intent["snapshot_id"])
    parent_id = intent.get("parent_snapshot_id")
    if store.current_snapshot_id() == snapshot_id:
        store.publish_current(str(parent_id))
    store.remove_snapshot(snapshot_id)
    ledger.delete_snapshot_audit(snapshot_id)
    ledger.delete_recovery_decision(attempt.attempt_id)


def _matches(existing: dict, attempt: Attempt, intent: dict) -> bool:
    return (
        existing["payload_hash"] == intent["payload_hash"]
        and existing["writer_id"] == attempt.writer_id
        and existing["idempotency_key"] == attempt.idempotency_key
        and existing["resulting_snapshot_id"] == intent["snapshot_id"]
        and existing["status"] == "accepted"
    )
