"""Evidence inspection for recovery: manifest, publication and confirmation."""

from __future__ import annotations

from lakecommit import conflicts, snapshot as snapshot_helpers
from lakecommit.conflicts import reason
from lakecommit.errors import StateError
from lakecommit.models import Attempt, Decision, FileEntry, Snapshot


def verify_intent(intent: dict) -> tuple[FileEntry, ...]:
    """Prove the journal payload is internally consistent before trusting it."""
    manifest = intent.get("manifest") or {}
    commit = intent.get("commit") or {}
    try:
        files = tuple(FileEntry.from_dict(item) for item in manifest["files"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError(
            "RECOVERY_JOURNAL_INVALID",
            "publish intent manifest is malformed",
            details={"attempt_id": intent.get("attempt_id")},
        ) from exc
    if (
        manifest.get("snapshot_id") != intent.get("snapshot_id")
        or manifest.get("parent_snapshot_id") != intent.get("parent_snapshot_id")
        or manifest.get("sequence") != intent.get("sequence")
        or commit.get("snapshot_id") != intent.get("snapshot_id")
        or commit.get("payload_hash") != intent.get("payload_hash")
        or snapshot_helpers.commit_hash(commit) != manifest.get("commit_hash")
        or snapshot_helpers.manifest_hash(files) != manifest.get("manifest_hash")
    ):
        raise StateError(
            "RECOVERY_EVIDENCE_CONFLICT",
            "publish intent manifest and commit disagree with its header fields",
            details={"attempt_id": intent.get("attempt_id")},
        )
    return files


def inspect_snapshot(store, intent: dict) -> Snapshot | None:
    snapshot_id = str(intent["snapshot_id"])
    if not store.has_snapshot(snapshot_id):
        return None
    snapshot = store.load_snapshot(snapshot_id)
    commit = store.load_commit(snapshot_id)
    if commit != intent["commit"] or snapshot.parent_snapshot_id != intent.get("parent_snapshot_id"):
        raise StateError(
            "RECOVERY_EVIDENCE_CONFLICT",
            "published snapshot does not match its persisted publish intent",
            details={"attempt_id": intent["attempt_id"], "snapshot_id": snapshot_id},
        )
    return snapshot


def confirmation(ledger, intent: dict) -> dict | None:
    """Return the first-result cache row only when it confirms this intent."""
    attempt = Attempt.from_dict(intent["attempt"])
    record = ledger.get_idempotency(attempt.writer_id, attempt.idempotency_key)
    if record is None or record["first_attempt_id"] != attempt.attempt_id:
        return None
    if record["payload_hash"] != intent["payload_hash"] or record["first_snapshot_id"] != intent["snapshot_id"]:
        raise StateError(
            "RECOVERY_EVIDENCE_CONFLICT",
            "idempotency cache confirms a different payload or snapshot for this publish intent",
            details={"attempt_id": attempt.attempt_id, "snapshot_id": intent["snapshot_id"]},
        )
    return record


def accepted_decision(intent: dict, reasons: list[dict]) -> Decision:
    return Decision.accepted(
        Attempt.from_dict(intent["attempt"]),
        str(intent["payload_hash"]),
        reasons,
        str(intent["snapshot_id"]),
        rebase=intent["commit"].get("rebase"),
    )


def completion_reasons(intent: dict, evidence: dict) -> list[dict]:
    rebase = intent["commit"].get("rebase")
    if rebase:
        first = reason(
            conflicts.INFO_SAFE_REBASE,
            "stale base accepted through a safe file-level rebase",
            severity="info",
            details=rebase,
        )
    else:
        first = reason(
            conflicts.INFO_CURRENT_BASE,
            "attempt was evaluated against the current snapshot",
            severity="info",
            details={"snapshot_id": intent.get("parent_snapshot_id")},
        )
    return [first, recovery_reason(intent, evidence)]


def recovery_reason(intent: dict, evidence: dict) -> dict:
    from lakecommit.recovery import RECOVERY_COMPLETED

    return reason(
        RECOVERY_COMPLETED,
        "unconfirmed publish intent was safely completed",
        severity="info",
        details={**evidence, "snapshot_id": intent["snapshot_id"]},
    )
