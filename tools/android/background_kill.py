"""Kill the app mid-transfer and check recovery: BackgroundKillTest `arm`, kill -9 if still alive, then `verify`.

Needs the controlled server and `adb reverse tcp:8765 tcp:8765`; the debug APKs must be installed.
"""

import argparse
import subprocess  # nosec B404
import sys
import time

PACKAGE = "pl.altani.spike"
RUNNER = f"{PACKAGE}.test/androidx.test.runner.AndroidJUnitRunner"
TEST = f"{PACKAGE}.offline.background.BackgroundKillTest"


def adb(*args: str) -> str:
    return subprocess.run(["adb", *args], capture_output=True, text=True, check=False).stdout  # nosec B603 B607


def instrument(phase: str) -> bool:
    out = adb("shell", "am", "instrument", "-w", "-e", "class", f"{TEST}#{phase}", "-e", "backgroundKill", phase, RUNNER)
    print(out)
    return "FAILURES!!!" not in out and "OK (1 test)" in out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", type=float, default=2.0, help="seconds between the kill and the verify run")
    args = parser.parse_args()
    if not instrument("arm"):
        print("arm failed")
        return 1
    pid = adb("shell", "pidof", PACKAGE).strip()
    if pid:
        adb("shell", "run-as", PACKAGE, "kill", "-9", pid)
        print(f"killed {pid}")
    else:
        print("process already ended with the instrumentation")
    time.sleep(args.wait)
    return 0 if instrument("verify") else 1


if __name__ == "__main__":
    sys.exit(main())
