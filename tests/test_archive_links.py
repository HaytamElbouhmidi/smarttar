"""SmartTar contained archive-link tests v0.2.0."""

import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import smarttar


class ArchiveLinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, files=(), links=(), directories=(), name="links.tar.gz"):
        path = self.root / name
        with tarfile.open(path, "w:gz") as archive:
            for filename in directories:
                member = tarfile.TarInfo(filename)
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            for filename, target, kind in links:
                member = tarfile.TarInfo(filename)
                member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                member.linkname = target
                archive.addfile(member)
            for filename, content in files:
                member = tarfile.TarInfo(filename)
                member.size = len(content)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(content))
        return path

    def extract(self, archive):
        info = smarttar.inspect_archive(archive)
        output = self.root / "output"
        smarttar.unpack_checked(info, output)
        return output

    def test_relative_file_symlink_round_trip(self):
        archive = self.archive(files=[("bundle/lib/libjvm.real", b"runtime")], links=[("bundle/lib/libjvm.so", "libjvm.real", "symlink")])
        output = self.extract(archive)
        link = output / "bundle/lib/libjvm.so"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "libjvm.real")
        self.assertEqual(link.read_bytes(), b"runtime")

    def test_internal_parent_relative_symlink_round_trip(self):
        archive = self.archive(files=[("bundle/jbr/lib/fontconfig", b"font")], links=[("bundle/jbr/conf/fonts", "../lib/fontconfig", "symlink")])
        output = self.extract(archive)
        self.assertEqual((output / "bundle/jbr/conf/fonts").read_bytes(), b"font")

    def test_symlink_to_implicit_directory_round_trip(self):
        archive = self.archive(files=[("bundle/runtime/lib/data", b"data")], links=[("bundle/jbr", "runtime", "symlink")])
        output = self.extract(archive)
        self.assertTrue((output / "bundle/jbr").is_symlink())
        self.assertEqual((output / "bundle/jbr/lib/data").read_bytes(), b"data")

    def test_hardlink_to_archive_root_relative_target(self):
        archive = self.archive(files=[("bundle/lib/source", b"shared")], links=[("bundle/lib/copy", "bundle/lib/source", "hardlink")])
        output = self.extract(archive)
        source = output / "bundle/lib/source"
        linked = output / "bundle/lib/copy"
        self.assertEqual(source.stat().st_ino, linked.stat().st_ino)
        self.assertEqual(linked.read_bytes(), b"shared")

    def test_forward_hardlink_chain_is_resolved_to_regular_file(self):
        archive = self.archive(files=[("source", b"shared")], links=[("first", "second", "hardlink"), ("second", "source", "hardlink")])
        output = self.extract(archive)
        self.assertEqual((output / "first").stat().st_ino, (output / "source").stat().st_ino)
        self.assertEqual((output / "second").read_bytes(), b"shared")

    def test_symlink_chain_round_trip(self):
        archive = self.archive(files=[("source", b"shared")], links=[("first", "second", "symlink"), ("second", "source", "symlink")])
        output = self.extract(archive)
        self.assertEqual((output / "first").read_bytes(), b"shared")

    def test_hardlink_target_through_symlink_resolves_to_regular_file(self):
        archive = self.archive(files=[("source", b"shared")], links=[("alias", "source", "symlink"), ("hard", "alias", "hardlink")])
        output = self.extract(archive)
        self.assertFalse((output / "hard").is_symlink())
        self.assertEqual((output / "hard").stat().st_ino, (output / "source").stat().st_ino)

    def test_link_resolution_applies_parent_after_directory_alias(self):
        archive = self.archive(
            files=[("bundle/real/deep/data", b"data"), ("bundle/other", b"selected")],
            links=[("bundle/alias", "real/deep", "symlink"), ("bundle/target", "alias/../../other", "symlink")],
        )
        output = self.extract(archive)
        self.assertEqual((output / "bundle/target").read_bytes(), b"selected")

    def test_absolute_links_are_rejected(self):
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                archive = self.archive(links=[("bad", "/etc/passwd", kind)])
                with self.assertRaisesRegex(smarttar.SmartTarError, "Unsafe archive link target"):
                    smarttar.inspect_archive(archive)

    def test_outside_relative_symlinks_are_rejected(self):
        for filename, target in (("bad", "../outside"), ("bundle/bad", "../../outside")):
            with self.subTest(filename=filename):
                archive = self.archive(links=[(filename, target, "symlink")])
                with self.assertRaisesRegex(smarttar.SmartTarError, "escapes"):
                    smarttar.inspect_archive(archive)

    def test_hardlink_parent_reference_is_root_relative(self):
        archive = self.archive(files=[("source", b"data")], links=[("bundle/bad", "../source", "hardlink")])
        with self.assertRaisesRegex(smarttar.SmartTarError, "escapes"):
            smarttar.inspect_archive(archive)

    def test_dangling_targets_are_rejected(self):
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                archive = self.archive(links=[("bad", "missing", kind)])
                with self.assertRaisesRegex(smarttar.SmartTarError, "Dangling"):
                    smarttar.inspect_archive(archive)

    def test_members_beneath_link_parents_are_rejected(self):
        archive = self.archive(files=[("real/data", b"data"), ("alias/overwrite", b"bad")], links=[("alias", "real", "symlink")])
        with self.assertRaisesRegex(smarttar.SmartTarError, "link parent"):
            smarttar.inspect_archive(archive)

    def test_hardlink_parents_are_rejected(self):
        archive = self.archive(files=[("source", b"data"), ("alias/file", b"bad")], links=[("alias", "source", "hardlink")])
        with self.assertRaisesRegex(smarttar.SmartTarError, "link parent"):
            smarttar.inspect_archive(archive)

    def test_link_chain_cycles_are_rejected(self):
        for kinds in (("symlink", "symlink"), ("hardlink", "hardlink"), ("symlink", "hardlink")):
            with self.subTest(kinds=kinds):
                archive = self.archive(links=[("first", "second", kinds[0]), ("second", "first", kinds[1])])
                with self.assertRaisesRegex(smarttar.SmartTarError, "Cyclic"):
                    smarttar.inspect_archive(archive)

    def test_ancestor_directory_link_cycle_is_rejected(self):
        archive = self.archive(files=[("bundle/data", b"data")], links=[("bundle/up", "..", "symlink")])
        with self.assertRaisesRegex(smarttar.SmartTarError, "traversal cycle"):
            smarttar.inspect_archive(archive)

    def test_mutual_directory_link_cycle_is_rejected(self):
        archive = self.archive(
            files=[("first/data", b"one"), ("second/data", b"two")],
            links=[("first/other", "../second", "symlink"), ("second/other", "../first", "symlink")],
        )
        with self.assertRaisesRegex(smarttar.SmartTarError, "traversal cycle"):
            smarttar.inspect_archive(archive)

    def test_hardlink_to_directory_is_rejected(self):
        archive = self.archive(files=[("directory/file", b"data")], links=[("bad", "directory", "hardlink")])
        with self.assertRaisesRegex(smarttar.SmartTarError, "not a regular file"):
            smarttar.inspect_archive(archive)

    def test_file_traversal_link_targets_are_rejected(self):
        for target in ("source/child", "source/../source", "source/", "source/."):
            with self.subTest(target=target):
                archive = self.archive(files=[("source", b"data")], links=[("bad", target, "symlink")])
                with self.assertRaisesRegex(smarttar.SmartTarError, "non-directory"):
                    smarttar.inspect_archive(archive)

    def test_control_characters_in_link_targets_are_rejected(self):
        archive = self.archive(links=[("bad", "target\nExec=unexpected", "symlink")])
        with self.assertRaisesRegex(smarttar.SmartTarError, "Unsafe archive link target"):
            smarttar.inspect_archive(archive)

    def test_long_link_chain_is_rejected_even_with_cached_reverse_order(self):
        links = [(f"link{index}", "source" if index == 40 else f"link{index + 1}", "symlink") for index in reversed(range(41))]
        archive = self.archive(files=[("source", b"data")], links=links)
        with self.assertRaisesRegex(smarttar.SmartTarError, "40 resolutions"):
            smarttar.inspect_archive(archive)

    def test_links_are_not_created_until_sha256_is_verified(self):
        archive = self.archive(files=[("source", b"old")], links=[("alias", "source", "symlink"), ("hard", "source", "hardlink")])
        info = smarttar.inspect_archive(archive)
        self.archive(files=[("source", b"new")], links=[("alias", "source", "symlink"), ("hard", "source", "hardlink")])
        with patch("smarttar.os.symlink") as symlink:
            with patch("smarttar.os.link") as hardlink:
                with self.assertRaisesRegex(smarttar.SmartTarError, "changed"):
                    smarttar.unpack_checked(info, self.root / "output")
        symlink.assert_not_called()
        hardlink.assert_not_called()
        self.assertFalse((self.root / "output").exists())

    def test_changed_link_target_is_rejected_before_creation(self):
        archive = self.archive(files=[("one", b"data"), ("two", b"data")], links=[("alias", "one", "symlink")])
        info = smarttar.inspect_archive(archive)
        self.archive(files=[("one", b"data"), ("two", b"data")], links=[("alias", "two", "symlink")])
        with self.assertRaisesRegex(smarttar.SmartTarError, "changed"):
            smarttar.unpack_checked(info, self.root / "output")
        self.assertFalse((self.root / "output").exists())

    def test_empty_directory_target_and_implicit_link_parent(self):
        archive = self.archive(directories=["bundle/empty"], links=[("bundle/links/empty", "../empty", "symlink")])
        output = self.extract(archive)
        self.assertTrue((output / "bundle/links/empty").is_dir())
        self.assertEqual(list((output / "bundle/links/empty").iterdir()), [])

    def test_gnu_longlink_metadata_round_trip(self):
        path = self.root / "gnu-links.tar.gz"
        name = "x" * 150
        with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as archive:
            link = tarfile.TarInfo("alias")
            link.type = tarfile.SYMTYPE
            link.linkname = name
            archive.addfile(link)
            target = tarfile.TarInfo(name)
            target.size = 4
            archive.addfile(target, io.BytesIO(b"data"))
        output = self.extract(path)
        self.assertEqual((output / "alias").read_bytes(), b"data")

    def test_symlink_member_payload_is_rejected(self):
        path = self.root / "link-payload.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            link = tarfile.TarInfo("alias")
            link.type = tarfile.SYMTYPE
            link.linkname = "source"
            link.size = 3
            archive.addfile(link, io.BytesIO(b"bad"))
        with self.assertRaisesRegex(smarttar.SmartTarError, "unexpected payload"):
            smarttar.inspect_archive(path)

    def test_implicit_directories_count_toward_filesystem_limit(self):
        archive = self.archive(files=[("one/two/three/data", b"data")])
        with patch.object(smarttar, "MAX_ENTRIES", 3):
            with self.assertRaisesRegex(smarttar.SmartTarError, "filesystem entry limit"):
                smarttar.inspect_archive(archive)


if __name__ == "__main__":
    unittest.main()
