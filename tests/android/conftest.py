import sys
import threading
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.android.controlled_server import Server  # noqa: E402


@pytest.fixture
def server():
    with Server(0) as srv:
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        yield srv
        # a held response must not keep shutdown waiting
        srv.state.release()
        srv.shutdown()
