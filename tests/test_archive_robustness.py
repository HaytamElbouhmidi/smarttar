"""SmartTar archive robustness tests v0.2.0."""

import gzip
import hashlib
import io
import os
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import smarttar


def executable_header():
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", header, 16, 2, 62, 1)
    struct.pack_into("<H", header, 52, 64)
    return bytes(header)


class ArchiveRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, entries, name="test.tar.gz"):
        path = self.root / name
        with tarfile.open(path, "w:gz", compresslevel=1) as archive:
            for filename, content, mode in entries:
                entry = tarfile.TarInfo(filename)
                entry.mode = mode
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        return path

    def test_inspection_records_whole_compressed_file_hash(self):
        archive = self.archive([("data", b"payload", 0o644)])
        info = smarttar.inspect_archive(archive)
        self.assertEqual(info.archive_sha256, hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_same_size_content_change_is_rejected_and_cleaned(self):
        archive = self.archive([("data", b"old-content", 0o644)])
        info = smarttar.inspect_archive(archive)
        self.archive([("data", b"new-content", 0o644)])
        destination = self.root / "output"
        with self.assertRaisesRegex(smarttar.SmartTarError, "changed"):
            smarttar.unpack_checked(info, destination)
        self.assertFalse(destination.exists())

    def test_existing_destination_is_preserved(self):
        archive = self.archive([("data", b"payload", 0o644)])
        info = smarttar.inspect_archive(archive)
        destination = self.root / "output"
        destination.mkdir()
        (destination / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(smarttar.SmartTarError, "already exists"):
            smarttar.unpack_checked(info, destination)
        self.assertEqual((destination / "keep.txt").read_text(), "keep")

    def test_extraction_reapplies_member_limit(self):
        archive = self.archive([("data", b"a", 0o644)])
        info = smarttar.inspect_archive(archive)
        self.archive([("data", b"a", 0o644), ("extra", b"b", 0o644)])
        with patch.object(smarttar, "MAX_ENTRIES", 1):
            with self.assertRaisesRegex(smarttar.SmartTarError, "entry limit"):
                smarttar.unpack_checked(info, self.root / "output")
        self.assertFalse((self.root / "output").exists())

    def test_extraction_reapplies_member_size_limit(self):
        archive = self.archive([("data", b"payload", 0o644)])
        info = smarttar.inspect_archive(archive)
        with patch.object(smarttar, "MAX_FILE_BYTES", 4):
            with self.assertRaisesRegex(smarttar.SmartTarError, "file limit"):
                smarttar.unpack_checked(info, self.root / "output")
        self.assertFalse((self.root / "output").exists())

    def test_control_characters_in_paths_are_rejected(self):
        for char in ("\n", "\r", "\t", "\x7f", "\u2028"):
            with self.subTest(char=repr(char)):
                archive = self.archive([("program" + char + "Exec=other", executable_header(), 0o755)])
                with self.assertRaisesRegex(smarttar.SmartTarError, "Unsafe archive path"):
                    smarttar.inspect_archive(archive)

    def test_four_byte_elf_is_not_a_portable_candidate(self):
        archive = self.archive([("program", b"\x7fELF", 0o755)])
        self.assertEqual(smarttar.inspect_archive(archive).binaries, ())

    def test_renamed_text_is_not_an_appimage(self):
        archive = self.archive([("program.AppImage", b"#!/bin/sh\nexit 0\n", 0o755)])
        self.assertEqual(smarttar.inspect_archive(archive).binaries, ())

    def test_valid_executable_header_is_a_candidate(self):
        archive = self.archive([("program", executable_header(), 0o755)])
        self.assertEqual(smarttar.inspect_archive(archive).binaries, ("program",))

    def test_gzip_footer_crc_is_verified_during_inspection(self):
        archive = self.archive([("data", b"payload", 0o644)])
        compressed = bytearray(archive.read_bytes())
        compressed[-8] ^= 0xFF
        archive.write_bytes(compressed)
        with self.assertRaisesRegex(smarttar.SmartTarError, "Cannot read archive"):
            smarttar.inspect_archive(archive)

    def test_gzip_footer_crc_is_verified_during_extraction(self):
        archive = self.archive([("data", b"payload", 0o644)])
        info = smarttar.inspect_archive(archive)
        compressed = bytearray(archive.read_bytes())
        compressed[-8] ^= 0xFF
        archive.write_bytes(compressed)
        with self.assertRaisesRegex(smarttar.SmartTarError, "Cannot read archive"):
            smarttar.unpack_checked(info, self.root / "output")
        self.assertFalse((self.root / "output").exists())

    def test_truncated_gzip_footer_is_rejected(self):
        archive = self.archive([("data", b"payload", 0o644)])
        archive.write_bytes(archive.read_bytes()[:-4])
        with self.assertRaisesRegex(smarttar.SmartTarError, "Cannot read archive"):
            smarttar.inspect_archive(archive)

    def test_gzip_trailing_junk_is_rejected(self):
        archive = self.archive([("data", b"payload", 0o644)])
        with archive.open("ab") as output:
            output.write(b"not-gzip")
        with self.assertRaisesRegex(smarttar.SmartTarError, "Cannot read archive"):
            smarttar.inspect_archive(archive)

    def test_oversized_pax_header_rejected_before_payload_allocation(self):
        path = self.root / "pax.tar.gz"
        member = tarfile.TarInfo("pax")
        member.type = tarfile.XHDTYPE
        member.size = smarttar.MAX_METADATA_BYTES + 1
        path.write_bytes(gzip.compress(member.tobuf() + b"\x00" * 1024))
        with self.assertRaisesRegex(smarttar.SmartTarError, "metadata header"):
            smarttar.inspect_archive(path)

    def test_sparse_archive_is_rejected(self):
        path = self.root / "sparse.tar.gz"
        member = tarfile.TarInfo("sparse")
        member.type = tarfile.GNUTYPE_SPARSE
        path.write_bytes(gzip.compress(member.tobuf() + b"\x00" * 1024))
        with self.assertRaisesRegex(smarttar.SmartTarError, "sparse"):
            smarttar.inspect_archive(path)

    def test_long_pax_filename_round_trips(self):
        filename = "bundle/" + "x" * 150 + ".txt"
        archive = self.archive([(filename, b"content", 0o644)])
        info = smarttar.inspect_archive(archive)
        smarttar.unpack_checked(info, self.root / "output")
        self.assertEqual((self.root / "output" / filename).read_bytes(), b"content")

    def test_truncated_tar_payload_inside_valid_gzip_is_rejected(self):
        path = self.root / "short-tar.tar.gz"
        member = tarfile.TarInfo("data")
        member.size = 1024 * 1024
        path.write_bytes(gzip.compress(member.tobuf() + b"short"))
        with self.assertRaisesRegex(smarttar.SmartTarError, "Cannot read archive"):
            smarttar.inspect_archive(path)

    def test_extract_uses_one_sequential_gzip_pass(self):
        payload = os.urandom(1024 * 1024)
        archive = self.archive([("data", payload, 0o644)])
        info = smarttar.inspect_archive(archive)
        inflated = [0]
        original = gzip._GzipReader.read

        def counted_read(reader, size=-1):
            data = original(reader, size)
            inflated[0] += len(data)
            return data

        with patch.object(gzip._GzipReader, "read", counted_read):
            smarttar.unpack_checked(info, self.root / "output")
        self.assertLess(inflated[0], len(payload) + 65536)
        self.assertEqual((self.root / "output/data").read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
