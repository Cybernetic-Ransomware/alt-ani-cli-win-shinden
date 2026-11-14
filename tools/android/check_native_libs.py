"""Checks ELF libraries in APKs and wheels for 16 KB alignment, including Chaquopy's zips nested in ``assets/``."""

import argparse
import io
import struct
import sys
import zipfile

PAGE = 0x4000
PT_LOAD = 1
_NESTED = (".zip", ".whl", ".apk", ".jar", ".imy")


def load_alignments(data: bytes) -> list[int] | None:
    """LOAD segment alignments of a 64- or 32-bit ELF; None when ``data`` is not an ELF file."""
    if data[:4] != b"\x7fELF":
        return None
    is64 = data[4] == 2
    order = "<" if data[5] == 1 else ">"
    if is64:
        (phoff,) = struct.unpack_from(order + "Q", data, 0x20)
        phentsize, phnum = struct.unpack_from(order + "HH", data, 0x36)
    else:
        (phoff,) = struct.unpack_from(order + "I", data, 0x1C)
        phentsize, phnum = struct.unpack_from(order + "HH", data, 0x2A)
    aligns = []
    for i in range(phnum):
        base = phoff + i * phentsize
        (p_type,) = struct.unpack_from(order + "I", data, base)
        if p_type != PT_LOAD:
            continue
        (align,) = struct.unpack_from(order + "Q" if is64 else order + "I", data, base + (0x30 if is64 else 0x1C))
        aligns.append(align)
    return aligns


def scan(archive: zipfile.ZipFile, prefix: str, report: list[tuple[str, int, bool]]) -> None:
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = f"{prefix}{info.filename}"
        lower = info.filename.lower()
        if lower.endswith(_NESTED):
            data = archive.read(info)
            if zipfile.is_zipfile(io.BytesIO(data)):
                with zipfile.ZipFile(io.BytesIO(data)) as nested:
                    scan(nested, f"{name}!/", report)
            continue
        if ".so" not in lower.rsplit("/", 1)[-1]:
            continue
        aligns = load_alignments(archive.read(info))
        if aligns is None:
            continue
        smallest = min(aligns, default=0)
        report.append((name, smallest, smallest >= PAGE and all(a % PAGE == 0 for a in aligns)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("archives", nargs="+")
    args = parser.parse_args(argv)
    report: list[tuple[str, int, bool]] = []
    for path in args.archives:
        with zipfile.ZipFile(path) as archive:
            scan(archive, f"{path}!/", report)
    for name, align, ok in report:
        print(f"{'ok  ' if ok else 'FAIL'} 0x{align:<6x} {name}")
    violations = sum(1 for *_, ok in report if not ok)
    print(f"{len(report)} ELF libraries, {violations} violations")
    if not report:
        print("no ELF libraries found", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
