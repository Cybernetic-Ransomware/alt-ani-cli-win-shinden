"""background_kill.py passes only when its own kill -9 ended the exact process `arm` reported."""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.android import background_kill as bk  # noqa: E402


class FakeDevice:
    def __init__(self, armed: str | None = "4242", alive: set[str] | None = None, kill_rc: int = 0, kill_works: bool = True):
        self.armed = armed
        self.alive = {"4242"} if alive is None else alive
        self.kill_rc = kill_rc
        self.kill_works = kill_works
        self.commands: list[tuple[str, ...]] = []

    def adb(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        out, rc = "", 0
        if args[-2:] == ("cat", bk.ARMED_MARKER):
            out, rc = (self.armed, 0) if self.armed else ("No such file", 1)
        elif args[:3] == ("shell", "pidof", bk.PACKAGE):
            out, rc = " ".join(sorted(self.alive)), 0 if self.alive else 1
        elif "kill" in args:
            rc = self.kill_rc
            if rc == 0 and self.kill_works:
                self.alive.discard(args[-1])
        return subprocess.CompletedProcess(args, rc, out, "")


class FakeArm:
    def __init__(self, ended: bool = False):
        self.ended = ended

    def poll(self) -> int | None:
        return 0 if self.ended else None


@pytest.fixture
def device(monkeypatch):
    def install(**kwargs) -> FakeDevice:
        fake = FakeDevice(**kwargs)
        monkeypatch.setattr(bk, "adb", fake.adb)
        monkeypatch.setattr(bk, "wait_until", lambda check, timeout, step=0.5: check())
        return fake

    return install


@pytest.mark.unit
class TestKillArmed:
    def test_the_armed_process_killed_and_gone_is_confirmed(self, device):
        fake = device()
        assert bk.kill_armed(FakeArm(), 1) is None
        assert ("shell", "run-as", bk.PACKAGE, "kill", "-9", "4242") in fake.commands

    def test_no_marker_from_an_arm_that_ended_is_not_a_kill(self, device):
        fake = device(armed=None)
        assert bk.kill_armed(FakeArm(ended=True), 1) == "arm ended before it held a transfer"
        assert not any("kill" in c for c in fake.commands)

    def test_a_process_already_gone_is_not_a_kill(self, device):
        device(alive=set())
        assert bk.kill_armed(FakeArm(), 1) == "armed process 4242 was already gone before the kill"

    def test_a_failed_kill_command_is_reported(self, device):
        device(kill_rc=1)
        assert bk.kill_armed(FakeArm(), 1).startswith("kill -9 4242 failed (1)")

    def test_a_process_that_survives_the_kill_is_reported(self, device):
        device(kill_works=False)
        assert bk.kill_armed(FakeArm(), 1) == "process 4242 still alive after kill -9"


class FakePopen:
    def __init__(self, out: str):
        self.out = out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def poll(self) -> int | None:
        return None

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self.out, ""


@pytest.mark.unit
class TestMain:
    def test_an_arm_that_passed_on_its_own_fails_the_run_without_verify(self, device, monkeypatch):
        fake = device()
        monkeypatch.setattr(bk.subprocess, "Popen", lambda *a, **k: FakePopen("OK (1 test)"))
        monkeypatch.setattr(sys, "argv", ["background_kill.py", "--wait", "0"])
        assert bk.main() == 1
        assert not any("verify" in c for c in fake.commands)

    def test_a_confirmed_kill_runs_verify(self, device, monkeypatch):
        fake = device()
        monkeypatch.setattr(bk.subprocess, "Popen", lambda *a, **k: FakePopen("shortMsg=Process crashed."))
        monkeypatch.setattr(sys, "argv", ["background_kill.py", "--wait", "0"])
        assert bk.main() == 1  # the fake verify prints no "OK (1 test)"
        assert any("verify" in c for c in fake.commands)
