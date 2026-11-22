"""Everything local works without an HTTP transport (no curl_cffi wheels on Android) and without the executor."""

import json
import os
import subprocess
import sys
from pathlib import Path

from alt_ani_core.testing import FakeTransport

from .conftest import SERIES, ep, make_v2


def test_local_state_never_creates_a_transport(tmp_path):
    def no_transport():
        raise AssertionError("a local call must not create the transport")

    client = make_v2(tmp_path, transport_factory=no_transport)
    assert client.create(push=False, storage={"kind": "fs", "root": str(tmp_path / "library")})["ok"]
    cursor = client.head()
    client.data("preferences_update", changes={"audio": "jp", "quality": "max:720"}, expected_revision=0)
    client.data("network_set", kind="cellular")
    begun = client.data("playback_begin", series=SERIES, episode=ep(1))
    session = begun["playback_session"]
    assert client.data("playback_position", playback_session=session, seq=1, position_ms=1000)["accepted"]
    client.data("playback_ended", playback_session=session, seq=2, position_ms=2000, duration_ms=2000)
    (job,) = client.enqueue((2,))
    client.data("queue_pause", job_id=job["job_id"], expected_revision=job["revision"])
    assert client.data("queue_snapshot")["jobs"][0]["state"] == "paused"
    assert client.data("library_list")["items"] == []
    assert client.data("watch_series", series_id="7")["episodes"][0]["watched"] is True
    events, _ = client.drain(cursor)
    assert events
    status = client.data("engine_status")
    assert status["engine_started"] is False
    assert client.data("executor_status")["state"] == "stopped"
    assert json.loads(client.bridge.shutdown(5))["data"]["ended"]


def test_bridge_imports_without_curl_cffi():
    script = (
        "import sys\n"
        "sys.modules['curl_cffi'] = None\n"
        "sys.modules['alt_ani_core.transport.curl'] = None\n"
        "import alt_ani_bridge\n"
        "print('ok')\n"
    )
    bridge_root = Path(__file__).parents[2] / "apps" / "android" / "app" / "src" / "main" / "python"
    env = {**os.environ, "PYTHONPATH": str(bridge_root)}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, env=env)
    assert result.stdout.strip() == "ok"
