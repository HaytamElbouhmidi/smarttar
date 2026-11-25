#!/usr/bin/env python3
"""SmartTar v0.3.0, a local archive and AppImage installer for KDE."""

# Copyright (c) 2026 Haytam Elbouhmidi
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


VERSION = "0.3.0"
DESKTOP_ID = "smarttar-handler.desktop"
MIME_TYPE = "application/x-compressed-tar"
APPIMAGE_MIME_TYPES = (
    "application/vnd.appimage",
    "application/x-iso9660-appimage",
    "application/x-appimage",
)
MIME_TYPES = (MIME_TYPE, *APPIMAGE_MIME_TYPES)
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 16 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024
MAX_APPIMAGE_BYTES = MAX_FILE_BYTES
MAX_METADATA_BYTES = 1024 * 1024
MAX_TOTAL_METADATA_BYTES = 16 * 1024 * 1024
MAX_TAR_OVERHEAD_BYTES = 384 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024


class SmartTarError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    executable: bool
    directory: bool
    link_type: str = ""
    link_target: str = ""


@dataclass(frozen=True)
class Inspection:
    archive: Path
    entries: tuple[Entry, ...]
    kind: str
    binaries: tuple[str, ...]
    expanded_size: int
    archive_sha256: str

    def as_dict(self) -> dict:
        return {
            "version": VERSION,
            "archive": str(self.archive),
            "kind": self.kind,
            "entry_count": len(self.entries),
            "expanded_bytes": self.expanded_size,
            "portable_candidates": list(self.binaries),
            "archive_sha256": self.archive_sha256,
        }


@dataclass(frozen=True)
class AppImageInspection:
    image: Path
    appimage_type: int
    architecture: str
    size: int
    fingerprint: tuple[int, ...]
    header: bytes

    def as_dict(self) -> dict:
        return {
            "version": VERSION,
            "image": str(self.image),
            "kind": "appimage",
            "appimage_type": self.appimage_type,
            "architecture": self.architecture,
            "size_bytes": self.size,
        }


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def app_root() -> Path:
    return Path.home() / ".local/opt/smarttar"


def application_dir() -> Path:
    return data_home() / "applications"


def normalize_member(name: str) -> str:
    if not name or name.startswith("/") or any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in name):
        raise SmartTarError(f"Unsafe archive path: {name!r}")
    if len(name.encode("utf-8", errors="surrogateescape")) > 4096:
        raise SmartTarError("Archive path exceeds 4096 bytes.")
    if ".." in name.split("/"):
        raise SmartTarError(f"Parent traversal in archive path: {name!r}")
    path = PurePosixPath(name)
    if len(path.parts) > 128:
        raise SmartTarError("Archive path exceeds 128 components.")
    if str(path) == ".":
        return "."
    if any(part in ("", ".", "..") for part in path.parts):
        raise SmartTarError(f"Unsafe archive path: {name!r}")
    return str(path)


def _link_components(target: str) -> tuple[str, ...]:
    if not target or target.startswith("/") or any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in target):
        raise SmartTarError(f"Unsafe archive link target: {target!r}")
    if len(target.encode("utf-8", errors="surrogateescape")) > 4096:
        raise SmartTarError("Archive link target exceeds 4096 bytes.")
    parts = tuple(part for part in target.split("/") if part != "")
    if len(parts) > 128:
        raise SmartTarError("Archive link target exceeds 128 components.")
    return parts


def check_structure(entries: list[Entry]) -> dict[str, str]:
    items = {entry.path: entry for entry in entries}
    if len(items) != len(entries):
        raise SmartTarError("Duplicate archive paths are not supported.")
    directories = {"."}
    non_directories = sum(not entry.directory for entry in entries)
    for entry in entries:
        if normalize_member(entry.path) != entry.path or entry.path == ".":
            raise SmartTarError(f"Unsafe manifest path: {entry.path!r}")
        if entry.link_type not in ("", "symlink", "hardlink") or (entry.link_type and entry.directory):
            raise SmartTarError(f"Invalid archive link type: {entry.path}")
        if entry.directory:
            directories.add(entry.path)
        parent = PurePosixPath(entry.path).parent
        while str(parent) != ".":
            other = items.get(str(parent))
            if other is not None and other.link_type:
                raise SmartTarError(f"Archive member has a link parent: {parent}")
            if other is not None and not other.directory:
                raise SmartTarError(f"File is used as a directory: {parent}")
            directories.add(str(parent))
            parent = parent.parent
        if len(directories) + non_directories > MAX_ENTRIES + 1:
            raise SmartTarError("Archive manifest exceeds the filesystem entry limit.")

    resolved_links: dict[str, str] = {}
    resolved_hops: dict[str, int] = {}

    def resolve(parts: tuple[str, ...], active: tuple[str, ...] = ()) -> tuple[str, int]:
        current: list[str] = []
        hops = 0
        for index, part in enumerate(parts):
            if part in ("", "."):
                continue
            if part == "..":
                if not current:
                    raise SmartTarError("Archive link target escapes the extraction directory.")
                current.pop()
                continue
            candidate = "/".join((*current, part))
            node = items.get(candidate)
            if node is None and candidate not in directories:
                raise SmartTarError(f"Dangling archive link target: {candidate}")
            if node is not None and node.link_type:
                if candidate in active:
                    raise SmartTarError(f"Cyclic archive link target: {candidate}")
                if len(active) >= 40:
                    raise SmartTarError("Archive link chain exceeds 40 resolutions.")
                if candidate not in resolved_links:
                    base = tuple(current) if node.link_type == "symlink" else ()
                    destination, target_hops = resolve(base + _link_components(node.link_target), active + (candidate,))
                    if node.link_type == "hardlink" and destination in directories:
                        raise SmartTarError(f"Hardlink target is not a regular file: {candidate}")
                    if node.link_target.endswith("/") and destination not in directories:
                        raise SmartTarError(f"Archive link traverses a non-directory: {candidate}")
                    resolved_links[candidate] = destination
                    resolved_hops[candidate] = target_hops + 1
                hops += resolved_hops[candidate]
                if hops > 40:
                    raise SmartTarError("Archive link chain exceeds 40 resolutions.")
                destination = resolved_links[candidate]
                current = [] if destination == "." else destination.split("/")
            else:
                current.append(part)
            if index < len(parts) - 1 and ("/".join(current) or ".") not in directories:
                raise SmartTarError(f"Archive link traverses a non-directory: {candidate}")
        return "/".join(current) or ".", hops

    for entry in entries:
        if entry.link_type:
            resolve(tuple(PurePosixPath(entry.path).parts))

    children: dict[str, set[str]] = {}
    for directory in directories:
        if directory != ".":
            parent = str(PurePosixPath(directory).parent)
            children.setdefault(parent, set()).add(directory)
    for entry in entries:
        target = resolved_links.get(entry.path)
        if entry.link_type == "symlink" and target in directories:
            parent = str(PurePosixPath(entry.path).parent)
            children.setdefault(parent, set()).add(target)
    states: dict[str, int] = {}
    for directory in directories:
        if states.get(directory):
            continue
        states[directory] = 1
        stack = [(directory, iter(children.get(directory, ())))]
        while stack:
            node, pending = stack[-1]
            child = next(pending, None)
            if child is None:
                states[node] = 2
                stack.pop()
            elif states.get(child) == 1:
                raise SmartTarError("Archive directory links create a traversal cycle.")
            elif not states.get(child):
                states[child] = 1
                stack.append((child, iter(children.get(child, ()))))
    return resolved_links


class _GuardedTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        archive._smarttar_headers = getattr(archive, "_smarttar_headers", 0) + 1
        if archive._smarttar_headers > MAX_ENTRIES * 3 + 16:
            raise SmartTarError("Archive contains too many tar headers.")
        metadata_types = {
            tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
            tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK,
        }
        if self.type in metadata_types:
            if not 0 <= self.size <= MAX_METADATA_BYTES:
                raise SmartTarError("Archive metadata header exceeds the 1 MiB limit.")
            archive._smarttar_metadata = getattr(archive, "_smarttar_metadata", 0) + self.size
            if archive._smarttar_metadata > MAX_TOTAL_METADATA_BYTES:
                raise SmartTarError("Archive metadata exceeds the 16 MiB total limit.")
            depth = getattr(archive, "_smarttar_extension_depth", 0) + 1
            if depth > 16:
                raise SmartTarError("Archive metadata headers are nested too deeply.")
            archive._smarttar_extension_depth = depth
            try:
                return super()._proc_member(archive)
            finally:
                archive._smarttar_extension_depth -= 1
        if self.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE):
            raise SmartTarError(f"Unsupported sparse or special archive member: {self.name!r}")
        if not 0 <= self.size <= MAX_FILE_BYTES:
            raise SmartTarError("Archive member exceeds the 8 GiB file limit.")
        return super()._proc_member(archive)

    def _proc_gnusparse_00(self, *args, **kwargs):
        raise SmartTarError("Sparse archive members are not supported.")

    def _proc_gnusparse_01(self, *args, **kwargs):
        raise SmartTarError("Sparse archive members are not supported.")

    def _proc_gnusparse_10(self, *args, **kwargs):
        raise SmartTarError("Sparse archive members are not supported.")


class _HashingReader:
    def __init__(self, source):
        self.source = source
        self.size = 0
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        request = min(size if size >= 0 else COPY_CHUNK_BYTES, MAX_ARCHIVE_BYTES - self.size + 1)
        data = self.source.read(request)
        self.size += len(data)
        if self.size > MAX_ARCHIVE_BYTES:
            raise SmartTarError("Archive exceeds the 8 GiB compressed size limit.")
        self.digest.update(data)
        return data


class _ExpandedReader:
    def __init__(self, source):
        self.source = source
        self.size = 0

    def read(self, size=-1):
        limit = MAX_EXPANDED_BYTES + MAX_TAR_OVERHEAD_BYTES
        request = min(size if size >= 0 else COPY_CHUNK_BYTES, limit - self.size + 1)
        data = self.source.read(request)
        self.size += len(data)
        if self.size > limit:
            raise SmartTarError("Decompressed tar stream exceeds its resource limit.")
        return data


@contextmanager
def _open_archive(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as raw:
        details = os.fstat(raw.fileno())
        if not stat.S_ISREG(details.st_mode):
            raise SmartTarError("The archive must be a regular file.")
        if details.st_size > MAX_ARCHIVE_BYTES:
            raise SmartTarError("Archive exceeds the 8 GiB compressed size limit.")
        hashed = _HashingReader(raw)
        with gzip.GzipFile(fileobj=hashed, mode="rb") as compressed:
            expanded = _ExpandedReader(compressed)
            with tarfile.open(fileobj=expanded, mode="r|", tarinfo=_GuardedTarInfo, bufsize=64 * 1024) as archive:
                yield archive, hashed
            while expanded.read(COPY_CHUNK_BYTES):
                pass


def _validated_entry(member: tarfile.TarInfo, seen: set[str], total_bytes: int) -> tuple[Entry | None, int]:
    name = normalize_member(member.name)
    if name == "." and member.isdir():
        return None, total_bytes
    if name == ".":
        raise SmartTarError("An archive file cannot replace its extraction directory.")
    if name in seen:
        raise SmartTarError(f"Duplicate archive path: {name}")
    seen.add(name)
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()) or member.issparse():
        raise SmartTarError(f"Unsupported sparse or special archive member: {name}")
    if not 0 <= member.size <= MAX_FILE_BYTES:
        raise SmartTarError("Archive member exceeds the 8 GiB file limit.")
    link_type = "symlink" if member.issym() else "hardlink" if member.islnk() else ""
    if (member.isdir() or link_type) and member.size:
        raise SmartTarError(f"Directory or link member contains unexpected payload: {name}")
    if link_type:
        _link_components(member.linkname)
    total_bytes += member.size
    if total_bytes > MAX_EXPANDED_BYTES:
        raise SmartTarError("Archive exceeds the 16 GiB expanded size limit.")
    return Entry(name, member.size, bool(member.mode & 0o111), member.isdir(), link_type, member.linkname if link_type else ""), total_bytes


def is_portable_candidate(member: tarfile.TarInfo, fileobj) -> bool:
    name = PurePosixPath(member.name)
    filename = name.name.lower()
    if not member.isfile() or not member.mode & 0o111:
        return False
    if filename.endswith(".so") or ".so." in filename:
        return False
    if any(part.lower() in {"lib", "lib64", "plugins", "libexec"} for part in name.parts[:-1]):
        return False
    stream = fileobj.extractfile(member)
    if stream is None:
        return False
    with stream:
        header = stream.read(64)
    if len(header) < 52 or header[:4] != b"\x7fELF" or header[4] not in (1, 2) or header[5] not in (1, 2) or header[6] != 1:
        return False
    byte_order = "<" if header[5] == 1 else ">"
    expected_size = 52 if header[4] == 1 else 64
    if len(header) < expected_size:
        return False
    elf_type, machine, version = struct.unpack_from(byte_order + "HHI", header, 16)
    header_size = struct.unpack_from(byte_order + "H", header, 40 if header[4] == 1 else 52)[0]
    return elf_type in (2, 3) and machine != 0 and version == 1 and header_size == expected_size


def inspect_archive(archive: Path) -> Inspection:
    archive = archive.expanduser().resolve(strict=True)
    if not archive.is_file() or not archive.name.lower().endswith((".tar.gz", ".tgz", ".pkg.tar.gz")):
        raise SmartTarError("Select a .tar.gz or .tgz archive.")

    entries: list[Entry] = []
    binaries: list[str] = []
    product_metadata: dict[str, dict] = {}
    studio_builds: set[str] = set()
    shell_launchers: set[str] = set()
    metadata_bytes = 0
    seen: set[str] = set()
    total_bytes = 0
    try:
        with _open_archive(archive) as (tar, identity):
            for index, member in enumerate(tar):
                if index >= MAX_ENTRIES:
                    raise SmartTarError("Archive exceeds the 100,000 entry limit.")
                entry, total_bytes = _validated_entry(member, seen, total_bytes)
                if entry is None:
                    continue
                entries.append(entry)
                basename = PurePosixPath(entry.path).name
                if member.isfile() and basename in {"product-info.json", "build.txt"}:
                    read_limit = 256 * 1024 if basename == "product-info.json" else 4096
                    if member.size <= read_limit and metadata_bytes + member.size <= 1024 * 1024:
                        metadata_bytes += member.size
                        with tar.extractfile(member) as stream:
                            contents = stream.read(read_limit + 1)
                        if basename == "product-info.json":
                            try:
                                product = json.loads(contents)
                                if isinstance(product, dict):
                                    product_metadata[entry.path] = product
                            except (ValueError, UnicodeError, RecursionError):
                                pass
                        elif contents.startswith(b"AI-"):
                            studio_builds.add(str(PurePosixPath(entry.path).parent))
                elif member.isfile() and member.mode & 0o111 and PurePosixPath(entry.path).parts[-2:] == ("bin", "studio.sh"):
                    with tar.extractfile(member) as stream:
                        first_line = stream.read(128).split(b"\n", 1)[0]
                    if first_line in {b"#!/bin/sh", b"#!/bin/bash", b"#!/usr/bin/env sh", b"#!/usr/bin/env bash"}:
                        shell_launchers.add(entry.path)
                elif is_portable_candidate(member, tar):
                    binaries.append(entry.path)
    except (tarfile.TarError, OSError, EOFError, zlib.error, RecursionError) as exc:
        raise SmartTarError(f"Cannot read archive: {exc}") from exc

    if not entries:
        raise SmartTarError("Archive contains no installable or extractable files.")
    check_structure(entries)
    names = {entry.path.lower() for entry in entries if not entry.directory}
    basenames = {PurePosixPath(name).name for name in names}
    studio_detected, studio_launchers = android_studio_launchers(entries, binaries, product_metadata, studio_builds, shell_launchers)
    if archive.name.lower().endswith(".pkg.tar.gz") and ".pkginfo" in basenames:
        kind = "arch-package"
    elif "pkgbuild" in basenames:
        kind = "pkgbuild"
    elif studio_detected:
        kind = "android-studio"
        binaries = list(studio_launchers)
    elif binaries:
        kind = "portable"
    elif "install.sh" in basenames or "setup.sh" in basenames:
        kind = "install-script"
    elif basenames.intersection({"makefile", "cmakelists.txt", "configure", "meson.build", "pyproject.toml", "package.json", "cargo.toml"}):
        kind = "source"
    else:
        kind = "files"
    return Inspection(archive, tuple(entries), kind, tuple(binaries), total_bytes, identity.digest.hexdigest())


def android_studio_launchers(entries: list[Entry], binaries: list[str], products: dict[str, dict], builds: set[str], scripts: set[str]) -> tuple[bool, tuple[str, ...]]:
    regular = {entry.path for entry in entries if not entry.directory and not entry.link_type}
    products = {
        str(PurePosixPath(path).parent): product
        for path, product in products.items()
        if product.get("productCode") == "AI" and len(PurePosixPath(path).parent.parts) <= 1
    }
    metadata_roots = {
        str(PurePosixPath(path).parent)
        for path in regular
        if PurePosixPath(path).name == "product-info.json" and str(PurePosixPath(path).parent) == "android-studio"
    }
    roots = set(products) or {root for root in builds if PurePosixPath(root).name == "android-studio"} or metadata_roots
    if not roots:
        return False, ()
    if len(roots) != 1:
        return True, ()
    root = roots.pop()

    def joined(relative: str) -> str:
        return str(PurePosixPath(root) / relative)

    if joined("jbr/bin/java") not in regular or not any(path.startswith(joined("lib") + "/") and path.endswith(".jar") for path in regular):
        return True, ()
    allowed = {"bin/studio": binaries, "bin/studio.sh": scripts}
    product = products.get(root)
    if product is None:
        if joined("product-info.json") in regular:
            return True, ()
        for relative, candidates in allowed.items():
            candidate = joined(relative)
            if candidate in candidates:
                return True, (candidate,)
        return True, ()
    launches = product.get("launch", [])
    if not isinstance(launches, list):
        return True, ()
    host = platform.machine().lower()
    aliases = {"x86_64": "amd64", "x64": "amd64", "arm64": "aarch64"}
    host = aliases.get(host, host)
    candidates = []
    for launch in launches:
        if not isinstance(launch, dict) or launch.get("os") != "Linux":
            continue
        arch = launch.get("arch")
        if arch is not None and (not isinstance(arch, str) or aliases.get(arch.lower(), arch.lower()) != host):
            continue
        relative = launch.get("launcherPath")
        if not isinstance(relative, str) or relative not in allowed:
            continue
        candidate = joined(relative)
        if candidate in regular and candidate in allowed[relative]:
            candidates.append(candidate)
    unique = tuple(dict.fromkeys(candidates))
    return True, unique if len(unique) == 1 else ()


def ensure_disk_space(info: Inspection | AppImageInspection, destination: Path) -> None:
    parent = destination.parent
    while not parent.exists():
        if parent == parent.parent:
            raise SmartTarError("Cannot find the destination filesystem.")
        parent = parent.parent
    required = (
        info.size if isinstance(info, AppImageInspection)
        else info.expanded_size + len(info.entries) * 4096
    ) + 64 * 1024 * 1024
    free = shutil.disk_usage(parent).free
    if free < required:
        raise SmartTarError(
            f"Not enough free disk space. Need approximately {required / (1024 ** 3):.2f} GiB "
            f"including overhead, but only {free / (1024 ** 3):.2f} GiB is available."
        )


def unpack_checked(info: Inspection, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise SmartTarError(f"Destination already exists: {destination}")
    resolved_links = check_structure(list(info.entries))
    ensure_disk_space(info, destination)
    created = False
    try:
        with _open_archive(info.archive) as (tar, identity):
            destination.mkdir(parents=True, mode=0o700)
            created = True
            seen: set[str] = set()
            total_bytes = 0
            processed = 0
            for index, member in enumerate(tar):
                if index >= MAX_ENTRIES:
                    raise SmartTarError("Archive exceeds the 100,000 entry limit.")
                entry, total_bytes = _validated_entry(member, seen, total_bytes)
                if entry is None:
                    continue
                if processed >= len(info.entries) or info.entries[processed] != entry:
                    raise SmartTarError("Archive contents changed since inspection. Please open it again.")
                processed += 1
                path = destination.joinpath(*PurePosixPath(entry.path).parts)
                if entry.directory:
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                if entry.link_type:
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise SmartTarError(f"Cannot read file: {entry.path}")
                with source, path.open("xb") as output:
                    copied = 0
                    while chunk := source.read(COPY_CHUNK_BYTES):
                        copied += len(chunk)
                        if copied > entry.size:
                            raise SmartTarError(f"Size changed during extraction: {entry.path}")
                        output.write(chunk)
                if copied != entry.size:
                    raise SmartTarError(f"Incomplete archive member: {entry.path}")
                path.chmod(0o755 if entry.executable else 0o644)
            if processed != len(info.entries):
                raise SmartTarError("Archive contents changed since inspection. Please open it again.")
        if identity.digest.hexdigest() != info.archive_sha256:
            raise SmartTarError("Archive contents changed since inspection. Please open it again.")
        for entry in info.entries:
            if entry.link_type == "hardlink":
                path = destination.joinpath(*PurePosixPath(entry.path).parts)
                source = destination.joinpath(*PurePosixPath(resolved_links[entry.path]).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, path, follow_symlinks=False)
        for entry in info.entries:
            if entry.link_type == "symlink":
                path = destination.joinpath(*PurePosixPath(entry.path).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(entry.link_target, path)
    except Exception as exc:
        if created:
            shutil.rmtree(destination)
        if isinstance(exc, (tarfile.TarError, gzip.BadGzipFile, EOFError, zlib.error, RecursionError)):
            raise SmartTarError(f"Cannot read archive: {exc}") from exc
        raise


def _file_fingerprint(details: os.stat_result) -> tuple[int, ...]:
    return (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_ctime_ns)


@contextmanager
def _open_appimage(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SmartTarError(f"Cannot open AppImage. Select the downloaded file, not a shortcut: {exc}") from exc
    with os.fdopen(descriptor, "rb", buffering=0) as source:
        details = os.fstat(source.fileno())
        if not stat.S_ISREG(details.st_mode):
            raise SmartTarError("The AppImage must be a regular file, not a folder or device.")
        if details.st_size > MAX_APPIMAGE_BYTES:
            raise SmartTarError("AppImage exceeds the 8 GiB file size limit.")
        yield source, details


def _appimage_header(header: bytes) -> tuple[int, str]:
    if (
        len(header) < 52 or header[:4] != b"\x7fELF"
        or header[4] not in (1, 2) or header[5] not in (1, 2) or header[6] != 1
    ):
        raise SmartTarError("This download does not have a valid Linux AppImage header.")
    if header[8:11] not in (b"AI\x01", b"AI\x02"):
        raise SmartTarError("AppImage format not recognized. SmartTar supports marked type 1 and type 2 AppImages.")
    expected_size = 52 if header[4] == 1 else 64
    if len(header) < expected_size:
        raise SmartTarError("The AppImage header is incomplete. Download the file again.")
    byte_order = "<" if header[5] == 1 else ">"
    elf_type, machine, version = struct.unpack_from(byte_order + "HHI", header, 16)
    header_size = struct.unpack_from(byte_order + "H", header, 40 if header[4] == 1 else 52)[0]
    if elf_type not in (2, 3) or version != 1 or header_size != expected_size:
        raise SmartTarError("This download does not have a valid Linux AppImage header.")
    architectures = {
        (3, 1): "i686", (62, 2): "x86_64", (40, 1): "armv7l",
        (183, 2): "aarch64", (243, 2): "riscv64",
    }
    architecture = architectures.get((machine, header[4]))
    if architecture is None or header[5] != 1:
        raise SmartTarError("This AppImage processor type is not supported by SmartTar.")
    return header[10], architecture


def _check_appimage_architecture(architecture: str) -> None:
    host = platform.machine().lower()
    aliases = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64", "i386": "i686", "i486": "i686", "i586": "i686"}
    host = aliases.get(host, host)
    if architecture != host:
        raise SmartTarError(
            f"This AppImage is for {architecture}, but this computer is {host}. "
            "Download the version for this computer. Cross-architecture and 32-bit compatibility are not enabled by SmartTar."
        )


def inspect_appimage(image: Path) -> AppImageInspection:
    image = image.expanduser().absolute()
    if image.suffix.lower() != ".appimage":
        raise SmartTarError("Select a .AppImage file.")
    desktop_string(str(image))
    slug_from_archive(image)
    with _open_appimage(image) as (source, details):
        header = source.read(64)
        appimage_type, architecture = _appimage_header(header)
        if details.st_size <= (52 if header[4] == 1 else 64):
            raise SmartTarError("The AppImage is incomplete. Download the file again.")
        fingerprint = _file_fingerprint(details)
        if _file_fingerprint(os.fstat(source.fileno())) != fingerprint:
            raise SmartTarError("AppImage changed during inspection. Please open it again.")
    _check_appimage_architecture(architecture)
    return AppImageInspection(image, appimage_type, architecture, details.st_size, fingerprint, header)


def install_appimage(info: AppImageInspection, root: Path | None = None, desktop_dir: Path | None = None) -> Path:
    _check_appimage_architecture(info.architecture)
    root = (root or app_root()).expanduser().absolute()
    desktop_dir = (desktop_dir or application_dir()).expanduser().absolute()
    slug = slug_from_archive(info.image)
    target = root / slug
    launcher = desktop_dir / f"smarttar-{slug}.desktop"
    if target.exists() or target.is_symlink() or launcher.exists() or launcher.is_symlink():
        raise SmartTarError(f"'{slug}' is already installed. Existing files will not be overwritten.")
    final_executable = target / "payload" / info.image.name
    exec_line = desktop_quote(str(final_executable))
    name = info.image.stem
    desktop_string(name)
    ensure_disk_space(info, target)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".smarttar-", dir=root))
    created = False
    try:
        payload = staging / "payload"
        payload.mkdir(mode=0o700)
        executable = payload / info.image.name
        digest = hashlib.sha256()
        with _open_appimage(info.image) as (source, details):
            if _file_fingerprint(details) != info.fingerprint:
                raise SmartTarError("AppImage changed since inspection. Please open it again.")
            copied = 0
            copied_header = bytearray()
            with executable.open("xb") as output:
                while chunk := source.read(min(COPY_CHUNK_BYTES, info.size - copied + 1)):
                    if len(copied_header) < len(info.header):
                        copied_header.extend(chunk[:len(info.header) - len(copied_header)])
                        if not info.header.startswith(copied_header):
                            raise SmartTarError("AppImage header changed since inspection. Please open it again.")
                    copied += len(chunk)
                    if copied > info.size:
                        raise SmartTarError("AppImage changed during installation. Please open it again.")
                    output.write(chunk)
                    digest.update(chunk)
            if copied != info.size or _file_fingerprint(os.fstat(source.fileno())) != info.fingerprint:
                raise SmartTarError("AppImage changed during installation. Please open it again.")
            if _file_fingerprint(info.image.stat(follow_symlinks=False)) != info.fingerprint:
                raise SmartTarError("AppImage was replaced during installation. Please open it again.")
        executable.chmod(0o755)
        (staging / "smarttar.json").write_text(
            json.dumps({
                "smarttar_version": VERSION,
                "kind": "appimage",
                "source": info.image.name,
                "entry": info.image.name,
                "appimage_type": info.appimage_type,
                "architecture": info.architecture,
                "size_bytes": info.size,
                "sha256": digest.hexdigest(),
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        target.mkdir(mode=0o700)
        created = True
        payload.rename(target / "payload")
        (staging / "smarttar.json").rename(target / "smarttar.json")
        write_desktop_file(launcher, name, exec_line, working_directory=final_executable.parent)
        return target
    except BaseException:
        if created:
            shutil.rmtree(target)
        raise
    finally:
        shutil.rmtree(staging)


def slug_from_archive(archive: Path) -> str:
    name = archive.name
    for suffix in (".pkg.tar.gz", ".tar.gz", ".tgz", ".appimage"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    slug = re.sub(r"[^a-z0-9.-]+", "-", name.lower()).strip(".-")[:64]
    if not slug or slug in {".", ".."}:
        raise SmartTarError("Download filename cannot be used as an application name.")
    return slug


def desktop_string(value: str) -> str:
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in value):
        raise SmartTarError("Control characters are not allowed in desktop entry values.")
    return value.replace("\\", "\\\\")


def desktop_quote(value: str) -> str:
    desktop_string(value)
    value = value.replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        value = value.replace(char, "\\" + char)
    return '"' + desktop_string(value) + '"'


def write_desktop_file(path: Path, name: str, exec_line: str, hidden: bool = False, working_directory: Path | None = None) -> None:
    desktop_string(exec_line)
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={desktop_string(name)}",
        "Comment=Install downloaded archives and AppImages" if hidden else "Comment=Portable application installed by SmartTar",
        f"Exec={exec_line}",
        "Icon=package-x-generic" if hidden else "Icon=application-x-executable",
        "Terminal=false",
        "Categories=Utility;",
    ]
    if hidden:
        lines.extend((f"MimeType={';'.join(MIME_TYPES)};", "NoDisplay=true", "TryExec=python3"))
    if working_directory is not None:
        lines.append(f"Path={desktop_string(str(working_directory))}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".smarttar-desktop-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
        os.chmod(temporary, 0o644)
        os.link(temporary, path, follow_symlinks=False)
    finally:
        os.unlink(temporary)


def install_portable(info: Inspection, candidate: str, root: Path | None = None, desktop_dir: Path | None = None) -> Path:
    if info.kind not in {"portable", "android-studio"} or candidate not in info.binaries:
        raise SmartTarError("This archive has no selected portable executable.")
    root = root or app_root()
    desktop_dir = desktop_dir or application_dir()
    slug = slug_from_archive(info.archive)
    target = root / slug
    launcher = desktop_dir / f"smarttar-{slug}.desktop"
    if target.exists() or target.is_symlink() or launcher.exists():
        raise SmartTarError(f"'{slug}' is already installed. Existing files will not be overwritten.")

    root.mkdir(parents=True, exist_ok=True)
    desktop_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".smarttar-", dir=root))
    created = False
    try:
        payload = staging / "payload"
        unpack_checked(info, payload)
        executable = payload.joinpath(*PurePosixPath(candidate).parts)
        if not executable.is_file() or executable.is_symlink():
            raise SmartTarError("Selected executable is missing after extraction.")
        (staging / "smarttar.json").write_text(
            json.dumps({"smarttar_version": VERSION, "archive": info.archive.name, "entry": candidate}, indent=2) + "\n",
            encoding="utf-8",
        )
        if target.exists() or launcher.exists():
            raise SmartTarError("Installation target appeared during extraction.")
        target.mkdir(mode=0o700)
        created = True
        (staging / "payload").rename(target / "payload")
        (staging / "smarttar.json").rename(target / "smarttar.json")
        final_executable = target / "payload" / Path(*PurePosixPath(candidate).parts)
        name = "Android Studio" if info.kind == "android-studio" else slug
        write_desktop_file(launcher, name, desktop_quote(str(final_executable)), working_directory=final_executable.parent)
        return target
    except Exception:
        if created and target.exists():
            shutil.rmtree(target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def run_command(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False, timeout=None if args[0] == "kdialog" else 30)
    except subprocess.TimeoutExpired as exc:
        raise SmartTarError(f"Command timed out: {args[0]}") from exc


def association_names() -> list[str]:
    names = ["mimeapps.list", "kde-mimeapps.list"]
    for item in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":"):
        desktop = item.lower()
        if re.fullmatch(r"[a-z0-9_-]{1,40}", desktop):
            name = f"{desktop}-mimeapps.list"
            if name not in names:
                names.append(name)
    return names


def valid_association_name(filename: str) -> bool:
    return filename == "mimeapps.list" or bool(re.fullmatch(r"[a-z0-9_-]{1,40}-mimeapps\.list", filename))


def default_line(path: Path, mime_type: str = MIME_TYPE) -> str | None:
    if not path.exists():
        return None
    section = False
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped == "[Default Applications]"
            continue
        if section and line.partition("=")[0].strip() == mime_type and "=" in line:
            return line
    return None


def write_default_line(path: Path, replacement: str | None, mime_type: str = MIME_TYPE) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    section = False
    section_start = None
    insert_at = len(lines)
    existing_at = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section and insert_at == len(lines):
                insert_at = index
            section = stripped == "[Default Applications]"
            if section:
                section_start = index
                insert_at = len(lines)
            continue
        if section and line.partition("=")[0].strip() == mime_type and "=" in line:
            existing_at = index
            break

    if existing_at is not None:
        if replacement is None:
            lines.pop(existing_at)
        else:
            if existing_at + 1 < len(lines) and not replacement.endswith("\n"):
                replacement += "\n"
            lines[existing_at] = replacement
    elif replacement is not None:
        if section_start is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            if lines and lines[-1].strip():
                lines.append("\n")
            lines.extend(("[Default Applications]\n", replacement))
        else:
            if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
                lines[insert_at - 1] += "\n"
            lines.insert(insert_at, replacement)

    new_text = "".join(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".smarttar-mime-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(new_text)
        os.chmod(temporary, (path.stat().st_mode & 0o777) if path.exists() else 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def refresh_kde_cache() -> list[str]:
    warnings = []
    if shutil.which("kbuildsycoca6") is not None:
        result = run_command("kbuildsycoca6", "--noincremental")
        if result.returncode:
            warnings.append("KDE cache refresh failed: " + (result.stderr.strip() or str(result.returncode)))
    return warnings


def configure_association(script_path: Path) -> bool:
    if shutil.which("xdg-mime") is None:
        raise SmartTarError("xdg-mime is required. Install xdg-utils first.")
    if shutil.which("kdialog") is None:
        raise SmartTarError("kdialog is required. Install the kdialog package first.")
    app_file = application_dir() / DESKTOP_ID
    settings = config_home() / "smarttar" / "association.json"
    if settings.exists():
        saved = json.loads(settings.read_text(encoding="utf-8"))
    else:
        saved = {}
    saved.setdefault("appimage", {})
    records = [(MIME_TYPE, saved)]
    records.extend((mime_type, saved["appimage"].setdefault(mime_type, {})) for mime_type in APPIMAGE_MIME_TYPES)
    for mime_type, record in records:
        if "previous_default" not in record:
            old = run_command("xdg-mime", "query", "default", mime_type)
            record["previous_default"] = old.stdout.strip() if old.returncode == 0 else ""
        record.setdefault("overrides", {})
        for filename in association_names():
            if filename not in record["overrides"]:
                file_path = config_home() / filename
                original_line = default_line(file_path, mime_type)
                if filename == "mimeapps.list" and original_line is not None:
                    if original_line.partition("=")[2].strip().split(";")[0] == DESKTOP_ID:
                        previous = record["previous_default"]
                        original_line = f"{mime_type}={previous};\n" if previous and previous != DESKTOP_ID else None
                record["overrides"][filename] = {
                    "previous_line": original_line,
                    "file_existed": file_path.exists(),
                }
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    if app_file.exists():
        app_file.unlink()
    write_desktop_file(
        app_file,
        "SmartTar",
        f"python3 {desktop_quote(str(script_path))} %f",
        hidden=True,
    )
    for mime_type, record in records:
        for filename in record["overrides"]:
            if not valid_association_name(filename):
                continue
            write_default_line(config_home() / filename, f"{mime_type}={DESKTOP_ID};\n", mime_type)
    for warning in refresh_kde_cache():
        print(f"Warning: {warning}", file=sys.stderr)
    recognized = True
    for mime_type in MIME_TYPES:
        current = run_command("xdg-mime", "query", "default", mime_type)
        if current.returncode != 0 or current.stdout.strip() != DESKTOP_ID:
            print(f"Warning: Default handler not confirmed for {mime_type}.", file=sys.stderr)
            recognized = False
    return recognized


def restore_association() -> None:
    settings = config_home() / "smarttar" / "association.json"
    app_file = application_dir() / DESKTOP_ID
    if settings.exists():
        saved = json.loads(settings.read_text(encoding="utf-8"))
        records = [(MIME_TYPE, saved)]
        records.extend((mime_type, saved.get("appimage", {})[mime_type]) for mime_type in APPIMAGE_MIME_TYPES if mime_type in saved.get("appimage", {}))
        created_files = set()
        for mime_type, association in records:
            if "overrides" in association:
                for filename, record in association["overrides"].items():
                    if not valid_association_name(filename):
                        continue
                    path = config_home() / filename
                    if not record.get("file_existed"):
                        created_files.add(path)
                    current_line = default_line(path, mime_type)
                    if current_line is None:
                        continue
                    current_ids = [item for item in current_line.partition("=")[2].strip().split(";") if item]
                    if DESKTOP_ID not in current_ids:
                        continue
                    if current_ids == [DESKTOP_ID]:
                        write_default_line(path, record.get("previous_line"), mime_type)
                    else:
                        remaining = [item for item in current_ids if item != DESKTOP_ID]
                        write_default_line(path, f"{mime_type}={';'.join(remaining)};\n", mime_type)
            else:
                current = run_command("xdg-mime", "query", "default", mime_type)
                previous = association.get("previous_default", "")
                if current.stdout.strip() == DESKTOP_ID and previous and previous != DESKTOP_ID:
                    result = run_command("xdg-mime", "default", previous, mime_type)
                    if result.returncode != 0:
                        raise SmartTarError(f"Could not restore the previous handler: {previous}")
        for path in created_files:
            if path.exists() and path.read_text(encoding="utf-8").strip() in ("", "[Default Applications]"):
                path.unlink()
    if app_file.exists():
        app_file.unlink()
    if settings.exists():
        settings.unlink()
    for warning in refresh_kde_cache():
        print(f"Warning: {warning}", file=sys.stderr)


def dialog(*args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("kdialog") is None:
        raise SmartTarError("kdialog is not installed. Install it with sudo pacman -S kdialog.")
    result = run_command("kdialog", "--title", f"SmartTar v{VERSION}", *args)
    if result.returncode not in (0, 1):
        raise SmartTarError("KDE dialog failed: " + (result.stderr.strip() or f"exit {result.returncode}"))
    return result


def notify(message: str, error: bool = False) -> None:
    result = dialog("--error" if error else "--msgbox", message)
    if result.returncode not in (0, 1):
        print(message, file=sys.stderr if error else sys.stdout)


def choose(prompt: str, options: list[tuple[str, str]]) -> str | None:
    args = ["--menu", prompt]
    for key, label in options:
        args.extend((key, label))
    result = dialog(*args)
    return result.stdout.strip() if result.returncode == 0 else None


def guide(info: Inspection) -> str:
    messages = {
        "arch-package": "Arch package detected. Extracting for inspection is available. A verified package can be installed separately using sudo pacman -U FILE.",
        "pkgbuild": "PKGBUILD detected. Inspect it and its sources before running makepkg -si in the extracted folder.",
        "install-script": "Installation script detected. SmartTar will not execute downloaded scripts automatically. Inspect the script and README first.",
        "source": "Source project detected. Build requirements and commands depend on the project. Inspect its README and build files.",
        "files": "No portable executable or supported package instructions were found. You can safely extract the files.",
        "portable": "Portable Linux executable detected. Installing copies the application to your home directory and creates an application launcher. It will run only when you launch it.",
        "android-studio": "Android Studio bundle detected. SmartTar keeps the bundled runtime and libraries together. Installation creates an Android Studio launcher without starting it. Complete Android Studio's setup wizard on first launch. If installation is not offered, the bundle's launcher or architecture is unsupported. Extract and review the publisher's instructions instead.",
    }
    return messages[info.kind]


def show_contents(info: Inspection) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", prefix="smarttar-", delete=False) as stream:
        temp = Path(stream.name)
        stream.write(f"Archive: {info.archive}\nType: {info.kind}\nExpanded size: {info.expanded_size} bytes\n\n")
        for entry in info.entries:
            label = "DIR " if entry.directory else "LINK" if entry.link_type else "FILE"
            target = f" -> {entry.link_target}" if entry.link_type else ""
            stream.write(f"{label} {entry.size:>12}  {entry.path}{target}\n")
    try:
        dialog("--textbox", str(temp), "900", "600")
    finally:
        temp.unlink(missing_ok=True)


def run_appimage_gui(image: Path) -> None:
    info = inspect_appimage(image)
    while True:
        selection = choose(
            f"{info.image.name}\nAppImage application\nProcessor: {info.architecture}\nSize: {info.size / (1024 * 1024):.1f} MiB",
            [("install", "Install AppImage"), ("guide", "How AppImage installation works"), ("cancel", "Cancel")],
        )
        if selection in (None, "cancel"):
            return
        if selection == "guide":
            notify(
                "An AppImage is a Linux program packaged as one file.\n\n"
                "SmartTar copies it to your app folder and adds a menu shortcut.\n"
                "The original download stays unchanged.\n"
                "The app does not run until you open its shortcut.\n\n"
                "Only install downloads from a publisher you trust.\n"
                "Format checks are not a virus scan.\n"
                "Some apps still need system libraries or FUSE support."
            )
        elif selection == "install":
            target = app_root() / slug_from_archive(info.image)
            confirm = dialog(
                "--yesno",
                f"Install: {info.image.name}\nDestination: {target}\n\n"
                "SmartTar will copy this AppImage, make the copy executable,\n"
                "and add an application-menu shortcut.\n"
                "Your original download stays unchanged.\n\n"
                "Only install software from a source you trust.\n"
                "This application can access your files when you launch it.\n"
                "It will not start during installation.",
            )
            if confirm.returncode == 0:
                installed = install_appimage(info)
                notify(
                    f"Installed successfully to:\n{installed}\n\n"
                    f"Open {info.image.stem} from the application menu to start it.\n"
                    "Your original AppImage download has not been changed."
                )
                return


def run_gui(archive: Path) -> None:
    if archive.suffix.lower() == ".appimage":
        run_appimage_gui(archive)
        return
    info = inspect_archive(archive)
    while True:
        options = []
        if info.kind in {"portable", "android-studio"} and info.binaries:
            options.append(("install", "Install Android Studio" if info.kind == "android-studio" else "Install portable application"))
        options.extend((("extract", "Extract to a new folder"), ("contents", "Review archive contents"), ("guide", "Installation guidance"), ("cancel", "Cancel")))
        selection = choose(
            f"{info.archive.name}\nDetected: {info.kind}\nFiles: {len(info.entries)}\nExpanded: {info.expanded_size / (1024 * 1024):.1f} MiB",
            options,
        )
        if selection in (None, "cancel"):
            return
        if selection == "guide":
            notify(guide(info))
        elif selection == "contents":
            show_contents(info)
        elif selection == "extract":
            picked = dialog("--getexistingdirectory", str(info.archive.parent))
            if picked.returncode != 0 or not picked.stdout.strip():
                continue
            target = Path(picked.stdout.strip()) / slug_from_archive(info.archive)
            confirm = dialog("--yesno", f"Extract {len(info.entries)} entries to:\n{target}\n\nUnsafe paths and links outside the archive are refused.")
            if confirm.returncode == 0:
                unpack_checked(info, target)
                notify(f"Extracted successfully to:\n{target}")
                return
        elif selection == "install":
            if len(info.binaries) == 1:
                executable = info.binaries[0]
            else:
                executable = choose("Select the application executable", [(name, name) for name in info.binaries])
            if executable is None:
                continue
            target = app_root() / slug_from_archive(info.archive)
            confirm = dialog(
                "--yesno",
                f"Install: {info.archive.name}\nExecutable: {executable}\nDestination: {target}\n\nOnly install software from a source you trust. The application may execute code when launched.",
            )
            if confirm.returncode == 0:
                installed = install_portable(info, executable)
                notify(f"Installed successfully to:\n{installed}\n\nThe app is now available in the application launcher.")
                return


def doctor() -> int:
    print(f"SmartTar v{VERSION} diagnostics (read-only)")
    print("Python: " + ".".join(str(part) for part in sys.version_info[:3]))
    print("Desktop: " + repr(os.environ.get("XDG_CURRENT_DESKTOP") or "unset"))
    healthy = sys.version_info >= (3, 10)
    available = {}
    for command in ("python3", "kdialog", "xdg-mime"):
        available[command] = shutil.which(command) is not None
        print(f"{command}: {'available' if available[command] else 'MISSING'}")
        healthy = healthy and available[command]
    print("kbuildsycoca6: " + ("available" if shutil.which("kbuildsycoca6") else "not found (optional)"))

    display = bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))
    print("Display session environment: " + ("present" if display else "MISSING"))
    healthy = healthy and display

    handler = application_dir() / DESKTOP_ID
    installed_script = data_home() / "smarttar" / "smarttar.py"
    print("Installed script: " + ("present" if installed_script.is_file() else "MISSING"))
    healthy = healthy and installed_script.is_file()
    handler_ok = handler.is_file()
    exec_ok = False
    try:
        if handler_ok and handler.stat().st_size <= 65536:
            fields = {}
            exec_count = 0
            section = False
            for line in handler.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    section = stripped == "[Desktop Entry]"
                elif section and "=" in line and not stripped.startswith("#"):
                    key, value = line.split("=", 1)
                    fields[key.strip()] = value
                    exec_count += key.strip() == "Exec"
            expected = f"python3 {desktop_quote(str(installed_script))} %f"
            exec_ok = exec_count == 1 and fields.get("Exec") == expected
            handler_ok = fields.get("Type") == "Application" and all(mime_type in fields.get("MimeType", "").split(";") for mime_type in MIME_TYPES)
        else:
            handler_ok = False
    except (OSError, ValueError, SmartTarError) as exc:
        print("Handler read error: " + repr(str(exc)))
        handler_ok = False
    print("Handler desktop entry: " + ("valid" if handler_ok else "MISSING OR INVALID"))
    print("Handler Exec: " + ("valid" if exec_ok else "INVALID"))
    healthy = healthy and handler_ok and exec_ok

    query_ok = bool(available["xdg-mime"])
    if available["xdg-mime"]:
        for mime_type in MIME_TYPES:
            try:
                result = run_command("xdg-mime", "query", "default", mime_type)
                current = result.stdout.strip()
                print(f"Default association ({mime_type}): {current!r} (query exit {result.returncode})")
                query_ok = query_ok and result.returncode == 0 and current == DESKTOP_ID
                if result.returncode != 0 and result.stderr.strip():
                    print("Query error: " + repr(result.stderr.strip()))
            except (OSError, SmartTarError) as exc:
                query_ok = False
                print(f"Default association query failed ({mime_type}): " + repr(str(exc)))
    else:
        print("Default association: not checked")
    healthy = healthy and query_ok
    print("AppImage launch compatibility: not verified (depends on the app and its runtime)")
    print("GUI rendering: not verified")
    print("Result: " + ("PASS" if healthy else "FAIL"))
    return 0 if healthy else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=f"SmartTar v{VERSION}, archive and AppImage installer for KDE Plasma")
    parser.add_argument("archive", type=Path, nargs="?", help="A .tar.gz, .tgz, or .AppImage download")
    parser.add_argument("--inspect", action="store_true", help="Print the download classification as JSON without running it")
    parser.add_argument("--extract", type=Path, metavar="PARENT", help="Extract safely into a new folder under PARENT")
    parser.add_argument("--setup", action="store_true", help="Register SmartTar for .tar.gz and .AppImage downloads")
    parser.add_argument("--doctor", action="store_true", help="Check dependencies and desktop configuration without making changes")
    parser.add_argument("--restore-association", action="store_true", help="Restore previous archive and AppImage handlers")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args()
    try:
        if args.doctor:
            return doctor()
        elif args.setup:
            recognized = configure_association(Path(__file__).resolve())
            if recognized:
                print("SmartTar is now the default handler for supported archives and AppImage MIME types.")
                print("If your file manager runs an AppImage directly, right-click it and choose Open With SmartTar.")
            else:
                print("SmartTar association was saved, but activation is not confirmed.")
                print("Run the installed smarttar.py with --doctor to check the current session.")
                return 2
        elif args.restore_association:
            restore_association()
            print("Previous archive and AppImage associations restored.")
        else:
            if args.archive is None:
                parser.error("an archive or AppImage path is required")
            if args.inspect:
                info = inspect_appimage(args.archive) if args.archive.suffix.lower() == ".appimage" else inspect_archive(args.archive)
                print(json.dumps(info.as_dict(), indent=2))
            elif args.extract is not None:
                if args.archive.suffix.lower() == ".appimage":
                    raise SmartTarError("AppImages are installed as one file, not extracted. Open this file with SmartTar to install it.")
                info = inspect_archive(args.archive)
                target = args.extract.expanduser().resolve(strict=True) / slug_from_archive(info.archive)
                unpack_checked(info, target)
                print(f"Extracted to {target}")
            else:
                run_gui(args.archive)
        return 0
    except (SmartTarError, OSError, ValueError) as exc:
        print(f"SmartTar: {exc}", file=sys.stderr)
        if not (args.inspect or args.extract is not None or args.setup or args.restore_association or args.doctor):
            try:
                notify(str(exc), error=True)
            except SmartTarError:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
