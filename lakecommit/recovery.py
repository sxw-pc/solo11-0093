"""Crash recovery for snapshot publications that were never confirmed."""

from __future__ import annotations

from dataclasses import dataclass

from lakecommit import config, journal, recovery_apply, recovery_evidence as evidence_helpers
from lakecommit.conflicts import reason
from lakecommit.errors import StateError
from lakecommit.models import Attempt, Decision

RECOVERY_COMPLETED = "RECOVERY_COMPLETED"
RECOVERY_ROLLED_BACK = "RECOVERY_ROLLED_BACK"
RECOVERY_ALREADY_CONFIRMED = "RECOVERY_ALREADY_CONFIRMED"


@dataclass
class RecoveryResult:
    attempt_id: str
    route: str
    resolution: str
    snapshot_id: str
    parent_snapshot_id: str | None
    payload_hash: str
    evidence: dict
    reasons: list[dict]
    decision: Decision | None = None

    def to_dict(self) -> dict:
        value = {
            "attempt_id": self.attempt_id,
            "route": self.route,
            "resolution": self.resolution,
            "snapshot_id": self.snapshot_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "payload_hash": self.payload_hash,
            "evidence": self.evidence,
            "reasons": self.reasons,
        }
        if self.decision is not None:
            value["decision"] = self.decision.to_dict()
        return value


class Recovery:
    """Resolve persisted publish intents into one stable, evidence-backed route."""

    def __init__(self, store, ledger, protocol: dict | None = None) -> None:
        self.store = store
        self.ledger = ledger
        self.protocol = protocol or {}
        requested = str(self.protocol.get("recovery_route", "complete"))
        self.requested_route = requested if requested in config.RECOVERY_ROUTES else "complete"

    def run(self) -> list[RecoveryResult]:
        pending = journal.intents(self.store)
        if len(pending) > 1:
            raise StateError(
                "RECOVERY_AMBIGUOUS",
                "more than one unconfirmed publish intent exists in this workdir",
                details={"attempt_ids": sorted(str(item["attempt_id"]) for item in pending)},
            )
        results = [self._resolve(intent) for intent in pending]
        if pending or self.store.current_path.is_file():
            self._scan_reachable()
        return results

    def _current_id(self) -> str | None:
        return self.store.current_snapshot_id()

    def _evidence(self, intent: dict, published: object | None, confirmed: bool) -> dict:
        return {
            "current_snapshot_id": self._current_id(),
            "parent_snapshot_id": intent.get("parent_snapshot_id"),
            "snapshot_published": published is not None,
            "confirmed": confirmed,
        }

    def _resolve(self, intent: dict) -> RecoveryResult:
        evidence_helpers.verify_intent(intent)
        published = evidence_helpers.inspect_snapshot(self.store, intent)
        confirmed = evidence_helpers.confirmation(self.ledger, intent) is not None
        evidence = self._evidence(intent, published, confirmed)
        if confirmed:
            return self._complete(intent, published, evidence, "already_confirmed")
        existing = journal.read_resolution(self.store, str(intent["attempt_id"]), str(intent["snapshot_id"]))
        if existing is not None:
            return self._reapply(intent, existing, published, confirmed)
        if self.requested_route == "complete":
            self._require_complete_safe(intent, published)
            return self._complete(intent, published, evidence, "completed")
        self._require_rollback_safe(intent)
        return self._rollback(intent, published, evidence)

    def _require_complete_safe(self, intent: dict, published: object | None) -> None:
        snapshot_id = str(intent["snapshot_id"])
        parent_id = intent.get("parent_snapshot_id")
        current = self._current_id()
        safe = published is not None and (
            current in {snapshot_id, parent_id} or self.store.is_ancestor(snapshot_id, current)
        )
        if not safe and published is None and current == parent_id and parent_id is not None:
            safe = self.store.has_snapshot(parent_id)
        if safe:
            return
        raise StateError(
            "RECOVERY_STATE_CONFLICT",
            "publish intent cannot be completed from the observed current pointer",
            details={"attempt_id": intent["attempt_id"], "snapshot_id": snapshot_id, "current_snapshot_id": current},
        )

    def _require_rollback_safe(self, intent: dict) -> None:
        snapshot_id = str(intent["snapshot_id"])
        parent_id = intent.get("parent_snapshot_id")
        current = self._current_id()
        parent_ok = parent_id is not None and self.store.has_snapshot(parent_id)
        safe = parent_ok and (current == snapshot_id or current == parent_id)
        if safe:
            return
        raise StateError(
            "RECOVERY_ROLLBACK_UNSAFE",
            "current pointer cannot be proven safe to return to the parent snapshot",
            details={"attempt_id": intent["attempt_id"], "snapshot_id": snapshot_id, "current_snapshot_id": current},
        )

    def _complete(
        self,
        intent: dict,
        published: object | None,
        evidence: dict,
        resolution_name: str,
    ) -> RecoveryResult:
        attempt = Attempt.from_dict(intent["attempt"])
        snapshot_id = str(intent["snapshot_id"])
        if resolution_name == "already_confirmed":
            reasons = [
                reason(
                    RECOVERY_ALREADY_CONFIRMED,
                    "publish intent was already confirmed by the ledger",
                    severity="info",
                    details=evidence,
                )
            ]
            # The intent may have survived the crash after confirmation; history stays.
            self._persist(intent, "complete", resolution_name, reasons, evidence)
            journal.clear_intent(self.store, attempt.attempt_id)
            journal.clear_resolution(self.store, attempt.attempt_id)
            return self._result(intent, "complete", resolution_name, reasons, evidence, evidence_helpers.accepted_decision(intent, reasons))
        reasons = evidence_helpers.completion_reasons(intent, evidence)
        self._persist(intent, "complete", resolution_name, reasons, evidence)
        recovery_apply.materialize_completion(self.store, self.ledger, intent, published, reasons)
        journal.clear_intent(self.store, attempt.attempt_id)
        return self._result(intent, "complete", resolution_name, reasons, evidence, evidence_helpers.accepted_decision(intent, reasons))

    def _rollback(self, intent: dict, published: object | None, evidence: dict) -> RecoveryResult:
        snapshot_id = str(intent["snapshot_id"])
        reasons = [
            reason(
                RECOVERY_ROLLED_BACK,
                "unconfirmed publish intent returned to its verified parent snapshot",
                severity="info",
                details={**evidence, "snapshot_id": snapshot_id},
            )
        ]
        self._persist(intent, "rollback", "rolled_back", reasons, evidence)
        recovery_apply.rollback_publication(self.store, self.ledger, intent)
        journal.clear_intent(self.store, str(intent["attempt_id"]))
        # Rolled back means pending retry: the next publication is a new incident.
        journal.clear_resolution(self.store, str(intent["attempt_id"]))
        return self._result(intent, "rollback", "rolled_back", reasons, evidence, None)

    def _reapply(
        self,
        intent: dict,
        resolution: dict,
        published: object | None,
        confirmed: bool,
    ) -> RecoveryResult:
        route = str(resolution["route"])
        stored_evidence = dict(resolution.get("evidence") or {})
        if route == "rollback":
            if confirmed:
                journal.clear_intent(self.store, str(intent["attempt_id"]))
                return self._result(
                    intent, "rollback", "already_confirmed", [], {**stored_evidence, "confirmed": True}, None
                )
            return self._rollback(intent, published, stored_evidence)
        return self._complete(intent, published, stored_evidence, str(resolution.get("resolution", "completed")))

    def _result(
        self,
        intent: dict,
        route: str,
        name: str,
        reasons: list[dict],
        evidence: dict,
        decision: Decision | None,
    ) -> RecoveryResult:
        return RecoveryResult(
            attempt_id=str(intent["attempt_id"]),
            route=route,
            resolution=name,
            snapshot_id=str(intent["snapshot_id"]),
            parent_snapshot_id=intent.get("parent_snapshot_id"),
            payload_hash=str(intent["payload_hash"]),
            evidence=evidence,
            reasons=reasons,
            decision=decision,
        )

    def _persist(self, intent: dict, route: str, name: str, reasons: list[dict], evidence: dict) -> None:
        attempt = Attempt.from_dict(intent["attempt"])
        if journal.read_resolution(self.store, attempt.attempt_id, str(intent["snapshot_id"])) is not None:
            return
        latest = self.ledger.get_recovery(attempt.attempt_id)
        if (
            latest is not None
            and latest["route"] == route
            and latest["resolution"] == name
            and latest["snapshot_id"] == intent["snapshot_id"]
            and latest["payload_hash"] == intent["payload_hash"]
        ):
            # The same incident is being re-applied after a crash inside recovery.
            return
        journal.write_resolution(
            self.store,
            {
                "attempt_id": attempt.attempt_id,
                "route": route,
                "resolution": name,
                "snapshot_id": intent["snapshot_id"],
                "parent_snapshot_id": intent.get("parent_snapshot_id"),
                "payload_hash": intent["payload_hash"],
                "reasons": reasons,
                "evidence": evidence,
            },
        )
        self.ledger.record_recovery(
            attempt_id=attempt.attempt_id,
            writer_id=attempt.writer_id,
            idempotency_key=attempt.idempotency_key,
            route=route,
            resolution=name,
            payload_hash=str(intent["payload_hash"]),
            snapshot_id=str(intent["snapshot_id"]),
            parent_snapshot_id=intent.get("parent_snapshot_id"),
            evidence=evidence,
            reasons=reasons,
        )

    def _scan_reachable(self) -> None:
        current = self._current_id()
        if current is None:
            raise StateError("CURRENT_POINTER_MISSING", "current pointer is missing during recovery")
        for snapshot in self.store.chain(current):
            if snapshot.sequence == 0:
                continue
            audit = self.ledger.has_snapshot_audit(snapshot.snapshot_id)
            confirmed = self.ledger.confirmed_snapshot(snapshot.snapshot_id)
            if audit and confirmed:
                continue
            raise StateError(
                "RECOVERY_EVIDENCE_MISSING",
                "a reachable snapshot has no matching audit or idempotency evidence",
                details={"snapshot_id": snapshot.snapshot_id, "audit": audit, "confirmed": confirmed},
            )


def recover(store, ledger, protocol: dict | None = None) -> list[RecoveryResult]:
    return Recovery(store, ledger, protocol).run()
