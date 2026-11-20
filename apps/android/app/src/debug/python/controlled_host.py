"""Debug builds only: Shinden and embed host traffic goes to the controlled server; the engine runs as in production."""

import ctypes
import errno
import os
import platform
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from http.cookiejar import CookieJar
from json import dumps, loads
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote, urlencode, urlsplit

from alt_ani_bridge import Bridge
from alt_ani_core import extract
from alt_ani_core.models import Stream, StreamProtocol, StreamVariant
from alt_ani_core.state import StorageLocator
from alt_ani_core.storage.api import (
    NO_ATOMIC_PUBLISH,
    TARGET_EXISTS,
    UNAVAILABLE,
    PlaybackTarget,
    from_os_error,
    storage_error,
)
from alt_ani_core.storage.fs import WORK_DIR, FsFileStore, fsync_dir, fsync_file
from alt_ani_core.transport.api import (
    EXTRACTOR_PROFILE,
    DecodeError,
    HttpStatusError,
    NetworkError,
    SessionProfile,
    Transport,
    TransportTimeout,
)

HOST = "controlled.test"
_ROUTES = {"shinden.pl": "/shinden", HOST: "/host"}


class _Response:
    # declared as HttpResponse declares them: protocol attributes are invariant
    status_code: int
    text: str
    headers: Mapping[str, str]
    url: str

    def __init__(self, status_code: int, body: bytes, headers: Mapping[str, str], url: str) -> None:
        self.status_code = status_code
        self.text = body.decode("utf-8", errors="replace")
        self.headers = headers
        self.url = url

    def json(self) -> Any:
        try:
            return loads(self.text)
        except ValueError:
            raise DecodeError(url=self.url) from None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HttpStatusError(self.status_code, url=self.url)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> urllib.request.Request | None:
        return None


class _Session:
    def __init__(self, base: str, profile: SessionProfile) -> None:
        self._base = base
        self._profile = profile
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()), _NoRedirect)

    def _target(self, url: str) -> str:
        parts = urlsplit(url)
        prefix = _ROUTES.get(parts.hostname or "")
        if parts.scheme != "https" or prefix is None:
            # anything else would leave the test environment
            raise NetworkError("host outside the controlled environment", url=url)
        return f"{self._base}{prefix}{parts.path}" + (f"?{parts.query}" if parts.query else "")

    def _send(self, method: str, url: str, headers: Mapping[str, str] | None, body: bytes | None) -> _Response:
        request = urllib.request.Request(self._target(url), data=body, method=method)
        for key, value in {**self._profile.headers, **(headers or {})}.items():
            request.add_header(key, value)
        try:
            with self._opener.open(request, timeout=self._profile.timeout) as reply:  # nosec B310 - http(s) base only
                return _Response(reply.status, reply.read(), dict(reply.headers.items()), url)
        except urllib.error.HTTPError as exc:
            return _Response(exc.code, exc.read(), dict(exc.headers.items()), url)
        except TimeoutError:
            raise TransportTimeout(url=url) from None
        except urllib.error.URLError, OSError:
            raise NetworkError(url=url) from None

    def get(self, url: str, *, headers: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None) -> _Response:
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        return self._send("GET", url, headers, None)

    def post(self, url: str, *, headers: Mapping[str, str] | None = None, json: Any = None, data: Any = None) -> _Response:
        body = dumps(json).encode() if json is not None else urlencode(data or {}).encode() or None
        return self._send("POST", url, headers, body)

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ControlledTransport:
    def __init__(self, base: str) -> None:
        if urlsplit(base).scheme not in ("http", "https"):
            raise ValueError("the controlled server needs an http(s) base URL")
        self._base = base.rstrip("/")

    def open(self, profile: SessionProfile) -> _Session:
        return _Session(self._base, profile)


def resolve(embed_url: str, referer: str, transport: Transport) -> Stream:
    """The controlled host's API lists its variants; quality labels are the server's, the choice stays the engine's."""
    online_id = urlsplit(embed_url).path.rsplit("/", 1)[-1]
    with transport.open(EXTRACTOR_PROFILE) as client:
        reply = client.get(f"https://{HOST}/api/{online_id}", headers={"Referer": referer})
        reply.raise_for_status()
        listed = reply.json()["variants"]
    variants = tuple(
        StreamVariant(
            url=v["url"],
            protocol=StreamProtocol(v["protocol"]),
            headers=v["headers"],
            label=v["label"],
            container="mp4" if v["protocol"] == "progressive" else None,
        )
        for v in listed
    )
    return Stream(variants=variants)


_RENAMEAT2 = {"x86_64": 316, "aarch64": 276}
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


def rename_noreplace(src: Path, dst: Path) -> None:
    """``renameat2(RENAME_NOREPLACE)``: atomic and never replaces ``dst``; raises FileExistsError when it exists."""
    number = _RENAMEAT2.get(platform.machine())
    if number is None:
        raise OSError(errno.ENOSYS, "renameat2 number unknown for this machine")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(
        ctypes.c_long(number),
        ctypes.c_int(_AT_FDCWD),
        ctypes.c_char_p(os.fsencode(src)),
        ctypes.c_int(_AT_FDCWD),
        ctypes.c_char_p(os.fsencode(dst)),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )
    if result != 0:
        code = ctypes.get_errno()
        raise (FileExistsError if code == errno.EEXIST else OSError)(code, os.strerror(code))


class AppStorageStore(FsFileStore):
    """App storage refuses hard links (SELinux denies ``link``), so on Linux publishing renames with RENAME_NOREPLACE."""

    def __init__(self, root: Path, authority: str | None = None, name: str | None = None) -> None:
        super().__init__(root)
        self._prefix = f"content://{authority}/{name}/" if authority and name else None
        self._linux = sys.platform in ("linux", "android")

    def check_root(self) -> None:
        if not self._linux:
            super().check_root()
            return
        work = self.root / WORK_DIR
        try:
            work.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise storage_error(UNAVAILABLE, errno=exc.errno) from None
        probe = work / f".probe-{uuid.uuid4().hex}"
        moved = probe.with_name(probe.name + "-moved")
        try:
            probe.write_bytes(b"")
            rename_noreplace(probe, moved)
        except OSError as exc:
            raise storage_error(NO_ATOMIC_PUBLISH, errno=exc.errno) from None
        finally:
            for path in (moved, probe):
                path.unlink(missing_ok=True)

    def publish(self, local: Path, target: StorageLocator) -> None:
        if not self._linux:
            super().publish(local, target)
            return
        dst = Path(super().playback_target(target).value)
        try:
            fsync_file(local)
            dst.parent.mkdir(parents=True, exist_ok=True)
            rename_noreplace(local, dst)
            fsync_dir(dst.parent)
        except FileExistsError:
            raise storage_error(TARGET_EXISTS) from None
        except OSError as exc:
            raise from_os_error(exc) from None

    def playback_target(self, locator: StorageLocator) -> PlaybackTarget:
        target = super().playback_target(locator)
        if self._prefix is None:
            return target
        relative = Path(target.value).relative_to(self.root).as_posix()
        return PlaybackTarget("content_uri", self._prefix + quote(relative, safe="/"))


def storage(bridge: Bridge, root: str, authority: str | None = None, name: str | None = None) -> str:
    """The ``storage`` argument for ``create_engine``; ``name`` is the content URI path of ``root`` under ``authority``."""
    reply = loads(bridge.register_platform_object(AppStorageStore(Path(root), authority, name)))
    return dumps({"kind": "platform", "handle": reply["data"]["handle"]})


def create_bridge(base: str) -> Bridge:
    extract.HOST_RULES[HOST] = extract.HostRule("custom", resolver=resolve)
    return Bridge(transport_factory=lambda: ControlledTransport(base))
