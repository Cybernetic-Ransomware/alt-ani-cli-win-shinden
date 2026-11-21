"""The device probe of the production store, run on a desktop Linux file system (hard links allowed there)."""

import json
import sys

import pytest
import storage_probe

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the probe drives renameat2 and symlinks")


@pytest.mark.unit
def test_every_store_guarantee_holds_on_linux(tmp_path):
    report = json.loads(storage_probe.run(str(tmp_path), 33))
    failed = {name: c for name, c in report["checks"].items() if not c["ok"] and name != "hard_links_refused"}
    assert failed == {}
    assert len(report["checks"]) == 11


@pytest.mark.unit
def test_below_api_30_only_the_gate_is_checked(tmp_path):
    report = json.loads(storage_probe.run(str(tmp_path), 29))
    assert set(report["checks"]) == {"hard_links_refused", "gate_below_api_30"}
    assert report["checks"]["gate_below_api_30"]["ok"]
