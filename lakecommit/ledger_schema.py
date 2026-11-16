"""SQLite schema for the audit ledger and recovery records."""

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
CREATE TABLE IF NOT EXISTS recovery_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL,
  route TEXT NOT NULL,
  resolution TEXT NOT NULL,
  snapshot_id TEXT,
  parent_snapshot_id TEXT,
  payload_hash TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  writer_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL
);
"""
