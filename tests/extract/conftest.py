import json
import socket
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from curl_cffi.curl import Curl

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "extract"


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch):
    """Fail fast if a mis-targeted Session patch would let a test reach libcurl or a raw socket."""

    def _blocked(*args, **kwargs):
        raise AssertionError("outbound network attempted in tests/extract")

    monkeypatch.setattr(Curl, "perform", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)


@pytest.fixture
def load_fixture():
    def _load(name: str):
        text = (FIXTURES_DIR / name).read_text(encoding="utf-8")
        return json.loads(text) if name.endswith(".json") else text

    return _load


def _fake_response(body):
    resp = MagicMock(status_code=200, **{"raise_for_status.return_value": None})
    if isinstance(body, str):
        resp.text = body
    else:
        resp.json.return_value = body
    return resp


@pytest.fixture
def fake_session(monkeypatch):
    """Serve bodies in call order: ``str`` as ``.text``, anything else as ``.json()``."""

    def _install(module, *, get=(), post=()):
        session = MagicMock()
        session.get.side_effect = [_fake_response(b) for b in get]
        session.post.side_effect = [_fake_response(b) for b in post]
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        monkeypatch.setattr(module.cffi_requests, "Session", MagicMock(return_value=session))
        return session

    return _install
