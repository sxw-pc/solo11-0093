"""Shared bench construction and crash injection for recovery tests."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from lakecommit import config, loader
from lakecommit.ledger import Ledger
from lakecommit.protocol import ProtocolEngine
from lakecommit.store import SnapshotStore


class SimulatedCrash(RuntimeError):
    """Raised by an injected interruption to emulate a killed process."""


def new_bench(tmp_path, scenario: str = "01_add_files_accepted.yaml"):
    bundle = loader.load_bundle(config.SCENARIOS_DIR / scenario)
    workdir = tmp_path / "bench"
    store = SnapshotStore(workdir)
    store.reset()
    initial = store.initialize(bundle.initial_snapshot, bundle.table.partition_columns)
    ledger = Ledger(store.ledger_path, reset=True)
    ledger.audit_snapshot(initial, store.load_commit(initial.snapshot_id))
    engine = ProtocolEngine(bundle.table, store, ledger, bundle.protocol)
    return SimpleNamespace(
        bundle=bundle,
        store=store,
        ledger=ledger,
        engine=engine,
        workdir=workdir,
        initial=initial,
    )


def crash_after(monkeypatch, target, method: str) -> None:
    """Run the real method, then abort exactly at the next durable boundary."""
    original = getattr(target, method)

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        raise SimulatedCrash(method)

    monkeypatch.setattr(target, method, wrapper)


def crash_before(monkeypatch, target, method: str) -> None:
    """Abort immediately before a durable boundary, leaving the prior step intact."""

    def wrapper(*args, **kwargs):
        raise SimulatedCrash(method)

    monkeypatch.setattr(target, method, wrapper)


def reopen(bench, protocol: dict | None = None):
    """Emulate a new process opening the same workdir."""
    bench.ledger.close()
    ledger = Ledger(bench.store.ledger_path)
    store = SnapshotStore(bench.workdir)
    engine = ProtocolEngine(bench.bundle.table, store, ledger, protocol or bench.bundle.protocol)
    return SimpleNamespace(
        bundle=bench.bundle,
        store=store,
        ledger=ledger,
        engine=engine,
        workdir=bench.workdir,
        initial=bench.initial,
    )


def attempt_with(bench, *, attempt_id: str | None = None, operation: dict | None = None):
    attempt = bench.bundle.attempts[0]
    changes: dict = {"logical_time": attempt.logical_time}
    if attempt_id:
        changes["attempt_id"] = attempt_id
    if operation is not None:
        changes["operation"] = operation
    return replace(attempt, **changes)
