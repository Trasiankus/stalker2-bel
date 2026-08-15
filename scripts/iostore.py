"""Minimal read-only UE5 IoStore (.utoc/.ucas) reader.

Enough of the format to locate a package by path and pull out its
.uasset / .uexp / .ubulk chunks.  Written because ZenTools.exe asserts on
S.T.A.L.K.E.R. 2's current container version.

Reference: Engine/Source/Runtime/Core/Private/IO/IoStore.cpp (UE 5.1).
"""

from __future__ import annotations

import ctypes
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

TOC_MAGIC = b"-==--==--==--==-"
NONE_INDEX = 0xFFFFFFFF

# EIoContainerFlags
FLAG_COMPRESSED = 1 << 0
FLAG_ENCRYPTED = 1 << 1
FLAG_SIGNED = 1 << 2
FLAG_INDEXED = 1 << 3

# EIoChunkType5 (UE5)
CHUNK_EXPORT_BUNDLE_DATA = 1
CHUNK_BULK_DATA = 2
CHUNK_OPTIONAL_BULK_DATA = 3
CHUNK_MEMORY_MAPPED_BULK_DATA = 4

CHUNK_TYPE_EXT = {
    CHUNK_EXPORT_BUNDLE_DATA: ".uasset",
    CHUNK_BULK_DATA: ".ubulk",
    CHUNK_OPTIONAL_BULK_DATA: ".uptnl",
    CHUNK_MEMORY_MAPPED_BULK_DATA: ".m.ubulk",
}


# --------------------------------------------------------------------------
# Oodle
# --------------------------------------------------------------------------

_oodle = None


def _load_oodle():
    """Lazily bind OodleLZ_Decompress from oo2core_9_win64.dll."""
    global _oodle
    if _oodle is not None:
        return _oodle

    here = Path(__file__).resolve().parent
    name = "oo2core_9_win64.dll"
    candidates = [
        Path(os.environ["OODLE_DLL"]) if os.environ.get("OODLE_DLL") else None,
        here.parent / "tools" / "UnrealReZen" / name,   # workspace layout
        here.parent / "tools" / name,                   # stalker2-bel repo layout
        here / "tools" / name,
        here / name,
    ]
    for c in filter(None, candidates):
        if c.is_file():
            fn = ctypes.WinDLL(str(c)).OodleLZ_Decompress
            fn.restype = ctypes.c_int64
            fn.argtypes = [
                ctypes.c_char_p, ctypes.c_int64,   # src, srcLen
                ctypes.c_char_p, ctypes.c_int64,   # dst, dstLen
                ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,  # fuzz, crc, verbosity
                ctypes.c_void_p, ctypes.c_int64,   # decBufBase, decBufSize
                ctypes.c_void_p, ctypes.c_void_p,  # callback, callbackUserData
                ctypes.c_void_p, ctypes.c_int64,   # scratch, scratchSize
                ctypes.c_int32,                    # threadPhase
            ]
            _oodle = fn
            return _oodle
    raise FileNotFoundError(
        "oo2core_9_win64.dll not found. Looked in:\n  "
        + "\n  ".join(str(c) for c in candidates if c)
        + "\nSet OODLE_DLL to point at it."
    )


def oodle_decompress(src: bytes, out_size: int) -> bytes:
    fn = _load_oodle()
    dst = ctypes.create_string_buffer(out_size)
    n = fn(src, len(src), dst, out_size, 1, 0, 0, None, 0, None, None, None, 0, 3)
    if n != out_size:
        raise RuntimeError(f"OodleLZ_Decompress returned {n}, expected {out_size}")
    return dst.raw[:out_size]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

class _Reader:
    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def fstring(self) -> str:
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:  # UTF-16
            n = -n
            raw = self.buf[self.pos:self.pos + n * 2]
            self.pos += n * 2
            return raw.decode("utf-16-le").rstrip("\x00")
        raw = self.buf[self.pos:self.pos + n]
        self.pos += n
        return raw.decode("utf-8", "replace").rstrip("\x00")


@dataclass
class TocHeader:
    version: int
    header_size: int
    entry_count: int
    block_count: int
    block_entry_size: int
    method_name_count: int
    method_name_length: int
    block_size: int
    dir_index_size: int
    partition_count: int
    container_flags: int
    partition_size: int
    perfect_hash_seed_count: int
    chunks_without_perfect_hash_count: int


@dataclass
class CompressionBlock:
    offset: int
    compressed_size: int
    uncompressed_size: int
    method_index: int


# --------------------------------------------------------------------------
# Container
# --------------------------------------------------------------------------

class IoStoreContainer:
    """One .utoc plus its .ucas partition(s)."""

    def __init__(self, utoc_path: str | os.PathLike):
        self.utoc_path = Path(utoc_path)
        self.mount_point = ""
        self.paths: dict[str, int] = {}      # full path -> toc entry index
        self._partitions: dict[int, object] = {}
        self._parse_toc()

    # -- parsing ----------------------------------------------------------

    def _parse_toc(self) -> None:
        data = self.utoc_path.read_bytes()
        if data[:16] != TOC_MAGIC:
            raise ValueError(f"{self.utoc_path} is not a .utoc file")

        fmt = "<BBHIIIIIIIII"
        (version, _r0, _r1, header_size, entry_count, block_count,
         block_entry_size, method_name_count, method_name_length,
         block_size, dir_index_size, partition_count) = struct.unpack_from(fmt, data, 16)

        off = 16 + struct.calcsize(fmt) + 8 + 16  # + container id + encryption guid
        container_flags, _r3, _r4, hash_seed_count = struct.unpack_from("<BBHI", data, off)
        off += struct.calcsize("<BBHI")
        partition_size, chunks_without_hash = struct.unpack_from("<QI", data, off)

        self.header = TocHeader(
            version, header_size, entry_count, block_count, block_entry_size,
            method_name_count, method_name_length, block_size, dir_index_size,
            partition_count, container_flags, partition_size,
            hash_seed_count, chunks_without_hash)

        if container_flags & FLAG_ENCRYPTED:
            raise NotImplementedError(
                f"{self.utoc_path.name} is AES-encrypted; not supported.")

        p = header_size

        # FIoChunkId: 8-byte package id | 2-byte BE index | pad | 1-byte type
        self.chunk_ids: list[tuple[int, int, int]] = []
        for _ in range(entry_count):
            raw = data[p:p + 12]
            p += 12
            self.chunk_ids.append((int.from_bytes(raw[0:8], "little"),
                                   int.from_bytes(raw[8:10], "big"),
                                   raw[11]))

        # FIoOffsetAndLength: 5-byte BE offset + 5-byte BE length
        self.offsets: list[tuple[int, int]] = []
        for _ in range(entry_count):
            self.offsets.append((int.from_bytes(data[p:p + 5], "big"),
                                 int.from_bytes(data[p + 5:p + 10], "big")))
            p += 10

        if version >= 4:                       # PerfectHash
            p += hash_seed_count * 4
            if version >= 5:                   # PerfectHashWithOverflow
                p += chunks_without_hash * 4

        # FIoStoreTocCompressedBlockEntry
        self.blocks: list[CompressionBlock] = []
        for _ in range(block_count):
            offset = int.from_bytes(data[p:p + 5], "little")
            csize = (int.from_bytes(data[p + 4:p + 8], "little") >> 8) & 0xFFFFFF
            usize = int.from_bytes(data[p + 8:p + 11], "little")
            self.blocks.append(CompressionBlock(offset, csize, usize, data[p + 11]))
            p += block_entry_size

        self.methods = ["None"]
        for _ in range(method_name_count):
            self.methods.append(
                data[p:p + method_name_length].split(b"\x00")[0].decode("ascii"))
            p += method_name_length

        if container_flags & FLAG_SIGNED:
            hash_size = struct.unpack_from("<I", data, p)[0]
            p += 4 + hash_size * 2 + block_count * 20

        if dir_index_size:
            self._parse_directory_index(data[p:p + dir_index_size])
            p += dir_index_size

        # chunk metas follow -- not needed

        # package id -> {chunk type: toc index}
        self.by_package: dict[int, dict[int, int]] = {}
        for i, (pkg, _idx, ctype) in enumerate(self.chunk_ids):
            self.by_package.setdefault(pkg, {})[ctype] = i

    def _parse_directory_index(self, buf: bytes) -> None:
        r = _Reader(buf)
        mount = r.fstring()
        while mount.startswith("../"):
            mount = mount[3:]
        self.mount_point = mount

        n = r.u32()
        # FIoDirectoryIndexEntry { Name, FirstChildEntry, NextSiblingEntry, FirstFileEntry }
        dirs = [struct.unpack_from("<IIII", buf, r.pos + i * 16) for i in range(n)]
        r.pos += n * 16

        n = r.u32()
        # FIoFileIndexEntry { Name, NextFileEntry, UserData }
        files = [struct.unpack_from("<III", buf, r.pos + i * 12) for i in range(n)]
        r.pos += n * 12

        n = r.u32()
        strings = [r.fstring() for _ in range(n)]

        def name(idx: int) -> str:
            return "" if idx == NONE_INDEX else strings[idx]

        stack = [(0, mount)] if dirs else []
        while stack:
            dir_idx, prefix = stack.pop()
            while dir_idx != NONE_INDEX:
                d_name, first_child, next_sibling, first_file = dirs[dir_idx]
                path = prefix if d_name == NONE_INDEX else f"{prefix}{name(d_name)}/"
                f = first_file
                while f != NONE_INDEX:
                    f_name, next_file, user_data = files[f]
                    self.paths[f"{path}{name(f_name)}"] = user_data
                    f = next_file
                if first_child != NONE_INDEX:
                    stack.append((first_child, path))
                dir_idx = next_sibling

    # -- data access ------------------------------------------------------

    def _partition(self, index: int):
        f = self._partitions.get(index)
        if f is None:
            suffix = "" if index == 0 else f"_s{index}"
            path = self.utoc_path.with_suffix("")
            path = path.with_name(path.name + suffix + ".ucas")
            f = open(path, "rb")
            self._partitions[index] = f
        return f

    def read_entry(self, index: int) -> bytes:
        offset, length = self.offsets[index]
        bs = self.header.block_size
        first = offset // bs
        last = (offset + length - 1) // bs
        out = bytearray()
        for bi in range(first, last + 1):
            b = self.blocks[bi]
            part = b.offset // self.header.partition_size if self.header.partition_size else 0
            f = self._partition(part)
            f.seek(b.offset - part * self.header.partition_size)
            raw = f.read(b.compressed_size)
            method = self.methods[b.method_index]
            if method == "None":
                chunk = raw[:b.uncompressed_size]
            elif method.lower() == "oodle":
                chunk = oodle_decompress(raw, b.uncompressed_size)
            elif method.lower() == "zlib":
                chunk = zlib.decompress(raw)
            else:
                raise NotImplementedError(f"compression method {method!r}")
            out += chunk
        start = offset - first * bs
        return bytes(out[start:start + length])

    def find(self, needle: str) -> list[str]:
        """Case-insensitive substring search over indexed paths."""
        low = needle.lower()
        return sorted(p for p in self.paths if low in p.lower())

    def package_chunks(self, path: str) -> dict[int, int]:
        """chunk type -> toc entry index, for the package at `path`."""
        toc_index = self.paths[path]
        package_id = self.chunk_ids[toc_index][0]
        return self.by_package[package_id]

    def close(self):
        for f in self._partitions.values():
            f.close()
        self._partitions.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
