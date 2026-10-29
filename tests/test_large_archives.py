"""SmartTar v0.2.0 large-archive acceptance and bounded-memory tests."""

import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import smarttar


MIB = 1024 * 1024
GIB = 1024 * MIB
ZERO_CHUNK = bytes(MIB)


class ZeroReader:
    """Produce a declared payload using at most one shared MiB of data."""

    def __init__(self, size):
        self.remaining = size

    def read(self, size=-1):
        amount = min(self.remaining, MIB, size if size >= 0 else MIB)
        self.remaining -= amount
        return ZERO_CHUNK[:amount]


def stored_gzip_tar(path, sizes):
    """Stored deflate blocks make a valid large .tar.gz without costly random data."""
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=0, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", bufsize=MIB, copybufsize=MIB) as archive:
                for index, size in enumerate(sizes):
                    member = tarfile.TarInfo(f"payload/part-{index}.bin")
                    member.size = size
                    member.mode = 0o644
                    archive.addfile(member, ZeroReader(size))


MEASURE_SCRIPT = r"""
import json
from pathlib import Path
import resource
import sys
import time

sys.path.insert(0, sys.argv[1])
import smarttar

started = time.perf_counter()
info = smarttar.inspect_archive(Path(sys.argv[2]))
inspected = time.perf_counter()
smarttar.unpack_checked(info, Path(sys.argv[3]))
extracted = time.perf_counter()
print(json.dumps({
    "version": smarttar.VERSION,
    "members": len(info.entries),
    "payload_bytes": info.expanded_size,
    "archive_sha256": info.archive_sha256,
    "inspect_seconds": round(inspected - started, 6),
    "extract_seconds": round(extracted - inspected, 6),
    "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
}))
"""


class LargeArchiveLimitTests(unittest.TestCase):
    def test_configured_limits_accept_archives_larger_than_one_gib(self):
        self.assertGreaterEqual(smarttar.MAX_ARCHIVE_BYTES, 8 * GIB)
        self.assertGreaterEqual(smarttar.MAX_EXPANDED_BYTES, 16 * GIB)

    def test_real_compressed_byte_limit_still_applies(self):
        with tempfile.TemporaryDirectory(prefix="smarttar-size-limit-") as temporary:
            archive = Path(temporary) / "stored.tar.gz"
            stored_gzip_tar(archive, [128 * 1024])
            self.assertGreater(archive.stat().st_size, 64 * 1024)
            with patch.object(smarttar, "MAX_ARCHIVE_BYTES", 64 * 1024):
                with self.assertRaisesRegex(smarttar.SmartTarError, "compressed size limit"):
                    smarttar.inspect_archive(archive)

    def test_real_expanded_byte_limit_still_applies(self):
        with tempfile.TemporaryDirectory(prefix="smarttar-size-limit-") as temporary:
            archive = Path(temporary) / "stored.tar.gz"
            stored_gzip_tar(archive, [32 * 1024, 32 * 1024])
            with patch.object(smarttar, "MAX_EXPANDED_BYTES", 48 * 1024):
                with self.assertRaisesRegex(smarttar.SmartTarError, "expanded size limit"):
                    smarttar.inspect_archive(archive)


@unittest.skipUnless(
    os.environ.get("SMARTTAR_RUN_LARGE_TESTS") == "1" and sys.platform.startswith("linux"),
    "Set SMARTTAR_RUN_LARGE_TESTS=1 on Linux for the real 1 GiB archive test",
)
class FullSizeArchiveTests(unittest.TestCase):
    def test_actual_gzip_over_one_gib_inspects_and_extracts_with_bounded_memory(self):
        with tempfile.TemporaryDirectory(prefix="smarttar-full-size-") as temporary:
            workspace = Path(temporary)
            archive = workspace / "one-gib-plus.tar.gz"
            destination = workspace / "extracted"
            started = time.perf_counter()
            stored_gzip_tar(archive, [512 * MIB, 512 * MIB])
            generation_seconds = time.perf_counter() - started
            archive_bytes = archive.stat().st_size
            self.assertGreater(archive_bytes, GIB)
            result = subprocess.run(
                [sys.executable, "-c", MEASURE_SCRIPT, str(Path(smarttar.__file__).parent), str(archive), str(destination)],
                capture_output=True,
                text=True,
                timeout=240,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            metrics = json.loads(result.stdout)
            self.assertEqual(metrics["members"], 2)
            self.assertEqual(metrics["payload_bytes"], GIB)
            self.assertEqual(len(metrics["archive_sha256"]), 64)
            self.assertLess(metrics["peak_rss_kib"], 128 * 1024)
            for index in range(2):
                path = destination / "payload" / f"part-{index}.bin"
                self.assertEqual(path.stat().st_size, 512 * MIB)
                with path.open("rb") as extracted:
                    self.assertEqual(extracted.read(4096), bytes(4096))
                    extracted.seek(-4096, 2)
                    self.assertEqual(extracted.read(), bytes(4096))
            metrics.update({
                "archive_bytes": archive_bytes,
                "generation_seconds": round(generation_seconds, 6),
                "fixture": "Valid stored-deflate gzip tar with two 512 MiB zero-filled regular files",
                "memory_scope": "Fresh Python inspection/extraction process, excluding fixture generation and GUI",
            })
            print("\nLARGE_ARCHIVE_METRICS " + json.dumps(metrics, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
