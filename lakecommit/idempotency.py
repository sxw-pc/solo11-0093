"""Payload identity and replay construction for idempotent submissions."""

from __future__ import annotations

import json

from lakecommit import conflicts
from lakecommit.conflicts import Code, reason
from lakecommit.fingerprint import object_fingerprint
from lakecommit.models import Attempt, Decision


def payload_hash(attempt: Attempt) -> str:
    """Stable identity of an attempt's intended effect.

    Attempt id and logical time are transport metadata and intentionally excluded.
    The base snapshot is part of the payload: a retry against another snapshot is
    a different request even when it uses the same writer/key pair.
    """
    return object_fingerprint(
        {
            "base_snapshot_id": attempt.base_snapshot_id,
            "operation": attempt.operation,
        }
    )


def mismatch_decision(attempt: Attempt, digest: str, first_attempt_id: str) -> Decision:
    return Decision.rejected(
        attempt,
        digest,
        [
            reason(
                Code.IDEMPOTENCY_PAYLOAD_MISMATCH,
                "writer/idempotency key was already used with a different payload",
                details={"first_attempt_id": first_attempt_id},
            )
        ],
        [Code.IDEMPOTENCY_PAYLOAD_MISMATCH],
        retry_required=False,
    )


def replay_decision(
    record: dict,
    attempt: Attempt,
    digest: str,
    replay_count: int,
) -> Decision:
    first = json.loads(record["first_decision_json"])
    first_attempt_id = str(record["first_attempt_id"])
    replay = {
        "replayed": True,
        "replay_of_attempt_id": first_attempt_id,
        "replay_count": replay_count,
    }
    reasons = list(first.get("reasons", []))
    reasons.append(
        reason(
            conflicts.INFO_IDEMPOTENT_REPLAY,
            "duplicate submission returned the first stored decision",
            severity="info",
            details={"first_attempt_id": first_attempt_id, "replay_count": replay_count},
        )
    )
    return Decision(
        attempt=attempt,
        status=str(first["status"]),
        outcome="replayed",
        payload_hash=digest,
        reasons=reasons,
        conflict_codes=list(first.get("conflict_codes", [])),
        retry_required=bool(first.get("retry_required", False)),
        rebase=first.get("rebase"),
        resulting_snapshot_id=first.get("resulting_snapshot_id"),
        snapshot_created=False,
        replay=replay,
    )
