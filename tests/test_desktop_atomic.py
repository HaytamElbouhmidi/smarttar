"""SmartTar v0.2.0 atomic launcher publishing regressions."""

import errno
import io
import os
import shutil
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import smarttar


class DesktopAtomicTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-desktop-atomic-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.launchers = self.root / "launchers"
        self.launcher = self.launchers / "smarttar-sample.desktop"
        self.apps = self.root / "apps"

    def assert_no_temporary_launchers(self):
        self.assertEqual(list(self.launchers.glob(".smarttar-desktop-*")), [])

    def fixture(self):
        program = shutil.which("true")
        if program is None:
            self.skipTest("A trusted Linux ELF fixture is required")
        payload = Path(program).read_bytes()
        archive = self.root / "sample.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo("app")
            member.mode = 0o755
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))
        return smarttar.inspect_archive(archive)

    def test_launcher_is_complete_and_has_expected_permissions(self):
        smarttar.write_desktop_file(self.launcher, "Sample", '"/example/app"')
        contents = self.launcher.read_text()
        self.assertTrue(contents.startswith("[Desktop Entry]\n"))
        self.assertIn('Exec="/example/app"\n', contents)
        self.assertEqual(stat.S_IMODE(self.launcher.stat().st_mode), 0o644)
        self.assert_no_temporary_launchers()

    def test_existing_launcher_is_not_overwritten(self):
        self.launchers.mkdir()
        self.launcher.write_text("Keep this launcher\n")
        with self.assertRaises(FileExistsError):
            smarttar.write_desktop_file(self.launcher, "Sample", '"/example/app"')
        self.assertEqual(self.launcher.read_text(), "Keep this launcher\n")
        self.assert_no_temporary_launchers()

    def test_existing_symlink_is_not_followed_or_replaced(self):
        self.launchers.mkdir()
        outside = self.root / "other-launcher.desktop"
        outside.write_text("Keep this target\n")
        self.launcher.symlink_to(outside)
        with self.assertRaises(FileExistsError):
            smarttar.write_desktop_file(self.launcher, "Sample", '"/example/app"')
        self.assertTrue(self.launcher.is_symlink())
        self.assertEqual(outside.read_text(), "Keep this target\n")
        self.assert_no_temporary_launchers()

    def test_permission_failure_leaves_no_launcher_and_install_can_retry(self):
        info = self.fixture()
        real_chmod = os.chmod

        def fail_temporary_permissions(path, *args, **kwargs):
            if Path(path).name.startswith(".smarttar-desktop-"):
                raise PermissionError(errno.EACCES, "Injected launcher permission failure")
            return real_chmod(path, *args, **kwargs)

        with patch.object(smarttar.os, "chmod", side_effect=fail_temporary_permissions):
            with self.assertRaises(PermissionError):
                smarttar.install_portable(info, "app", self.apps, self.launchers)
        self.assertFalse(self.launcher.exists())
        self.assertFalse((self.apps / "sample").exists())
        self.assert_no_temporary_launchers()
        installed = smarttar.install_portable(info, "app", self.apps, self.launchers)
        self.assertTrue((installed / "payload/app").is_file())
        self.assertTrue(self.launcher.is_file())

    def test_partial_write_failure_leaves_no_launcher_and_install_can_retry(self):
        info = self.fixture()
        real_fdopen = os.fdopen

        class FailingWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, contents):
                self.stream.write(contents[:8])
                self.stream.flush()
                raise OSError(errno.ENOSPC, "Injected launcher write failure")

        def fail_text_write(descriptor, mode="r", *args, **kwargs):
            stream = real_fdopen(descriptor, mode, *args, **kwargs)
            return FailingWriter(stream) if mode == "w" else stream

        with patch.object(smarttar.os, "fdopen", side_effect=fail_text_write):
            with self.assertRaisesRegex(OSError, "Injected launcher write failure"):
                smarttar.install_portable(info, "app", self.apps, self.launchers)
        self.assertFalse(self.launcher.exists())
        self.assertFalse((self.apps / "sample").exists())
        self.assert_no_temporary_launchers()
        installed = smarttar.install_portable(info, "app", self.apps, self.launchers)
        self.assertTrue((installed / "payload/app").is_file())
        self.assertTrue(self.launcher.is_file())

    def test_racing_launcher_is_preserved_and_application_is_rolled_back(self):
        info = self.fixture()
        real_link = os.link

        def create_competing_launcher(source, destination, *args, **kwargs):
            if Path(destination) == self.launcher:
                self.launcher.write_text("Concurrent launcher\n")
            return real_link(source, destination, *args, **kwargs)

        with patch.object(smarttar.os, "link", side_effect=create_competing_launcher):
            with self.assertRaises(FileExistsError):
                smarttar.install_portable(info, "app", self.apps, self.launchers)
        self.assertEqual(self.launcher.read_text(), "Concurrent launcher\n")
        self.assertFalse((self.apps / "sample").exists())
        self.assert_no_temporary_launchers()


if __name__ == "__main__":
    unittest.main()
