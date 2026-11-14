"""Builds the process's one ``Bridge`` for the Android host; everything platform-specific about Python is decided here."""

import platform
import sys

from alt_ani_bridge import Bridge
from alt_ani_core import dom
from alt_ani_core.dom import stdlib_backend


def create_bridge() -> Bridge:
    # selectolax has no Android wheel; the stdlib backend passes the same parser tests
    dom.set_backend(stdlib_backend.parse)
    # without curl_cffi (emulator flavor) the first remote request fails with transport_unavailable
    return Bridge()


def environment() -> str:
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        transport = "none"
    else:
        transport = "curl_cffi"
    return f"Python {platform.python_version()} on {platform.machine()}, transport {transport}, {sys.getfilesystemencoding()}"
