"""Kill the app mid-transfer and check recovery: BackgroundKillTest `arm`, kill -9 of its process, then `verify`.

`arm` holds a transfer and keeps its process alive, writing its pid to a marker; the run fails unless this script's
kill succeeded and that exact process is gone. Needs the controlled server and `adb reverse tcp:8765 tcp:8765`; the
debug APKs must be installed.
"""

import argparse
import subprocess  # nosec B404
import sys
import time

PACKAGE = "pl.altani.spike"
RUNNER = f"{PACKAGE}.test/androidx.test.runner.AndroidJUnitRunner"
TEST = f"{PACKAGE}.offline.background.BackgroundKillTest"
ARMED_MARKER = "files/background-kill-armed"


def adb(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["adb", *args], capture_output=True, text=True, check=False)  # nosec B603 B607


def instrument_args(phase: str) -> list[str]:
    return ["adb", "shell", "am", "instrument", "-w", "-e", "class", f"{TEST}#{phase}", "-e", "backgroundKill", phase, RUNNER]


def passed(out: str) -> bool:
    return "FAILURES!!!" not in out and "OK (1 test)" in out


def pids() -> set[str]:
    return set(adb("shell", "pidof", PACKAGE).stdout.split())


def wait_until(check, timeout: float, step: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(step)
    return check()


def armed_pid() -> str | None:
    reply = adb("shell", "run-as", PACKAGE, "cat", ARMED_MARKER)
    pid = reply.stdout.strip()
    return pid if reply.returncode == 0 and pid.isdigit() else None


def kill_armed(arm: subprocess.Popen[str], timeout: float) -> str | None:
    """Returns why the kill cannot be confirmed, or None once the armed process was killed by this script."""
    if not wait_until(lambda: armed_pid() is not None or arm.poll() is not None, timeout):
        return "arm never reported a held transfer"
    pid = armed_pid()
    if pid is None:
        return "arm ended before it held a transfer"
    if pid not in pids():
        return f"armed process {pid} was already gone before the kill"
    killed = adb("shell", "run-as", PACKAGE, "kill", "-9", pid)
    if killed.returncode != 0:
        return f"kill -9 {pid} failed ({killed.returncode}): {killed.stderr.strip() or killed.stdout.strip()}"
    if not wait_until(lambda: pid not in pids(), 10):
        return f"process {pid} still alive after kill -9"
    print(f"killed {pid}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", type=float, default=2.0, help="seconds between the kill and the verify run")
    parser.add_argument("--arm-timeout", type=float, default=90.0, help="seconds to wait for arm to hold a transfer")
    args = parser.parse_args()
    # a marker left by an earlier run would name a pid this run never armed
    adb("shell", "run-as", PACKAGE, "rm", "-f", ARMED_MARKER)
    with subprocess.Popen(instrument_args("arm"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as arm:  # nosec B603 B607
        problem = kill_armed(arm, args.arm_timeout)
        try:
            out, _ = arm.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            arm.kill()
            out, _ = arm.communicate()
    print(out)
    if problem is None and passed(out):
        problem = "arm finished on its own"
    if problem:
        print(f"kill not confirmed: {problem}")
        return 1
    time.sleep(args.wait)
    verify = adb(*instrument_args("verify")[1:])
    print(verify.stdout)
    return 0 if passed(verify.stdout) else 1


if __name__ == "__main__":
    sys.exit(main())
