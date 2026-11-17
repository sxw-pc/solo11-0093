from pathlib import Path
from types import SimpleNamespace

import pytest

from lakecommit import config, loader, runner
from lakecommit.ledger import Ledger
from lakecommit.protocol import ProtocolEngine
from lakecommit.store import SnapshotStore


@pytest.fixture
def bundle():
    return loader.load_bundle(config.SCENARIOS_DIR / "01_add_files_accepted.yaml")


@pytest.fixture
def bench(tmp_path, bundle):
    workdir = tmp_path / "bench"
    store = SnapshotStore(workdir)
    store.reset()
    initial = store.initialize(bundle.initial_snapshot, bundle.table.partition_columns)
    ledger = Ledger(store.ledger_path, reset=True)
    ledger.audit_snapshot(initial, store.load_commit(initial.snapshot_id))
    engine = ProtocolEngine(bundle.table, store, ledger, bundle.protocol)
    value = SimpleNamespace(
        bundle=bundle,
        store=store,
        ledger=ledger,
        engine=engine,
        workdir=workdir,
        initial=initial,
    )
    yield value
    ledger.close()


@pytest.fixture
def run_workdir(tmp_path):
    workdir = tmp_path / "run"
    runner.run_scenario(config.SCENARIOS_DIR / "01_add_files_accepted.yaml", workdir)
    return Path(workdir)
