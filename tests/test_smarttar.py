"""SmartTar v0.2.0 archive and installation tests."""

import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smarttar import (
    SmartTarError,
    desktop_quote,
    configure_association,
    default_line,
    inspect_archive,
    install_portable,
    restore_association,
    slug_from_archive,
    unpack_checked,
    write_default_line,
)


class SmartTarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.program = Path(shutil.which("true") or "/bin/true").read_bytes()

    def archive(self, filename="sample.tar.gz", files=(), links=()):
        path = self.root / filename
        with tarfile.open(path, "w:gz") as stream:
            for name, data, mode in files:
                raw = data.encode() if isinstance(data, str) else data
                entry = tarfile.TarInfo(name)
                entry.mode = mode
                entry.size = len(raw)
                stream.addfile(entry, io.BytesIO(raw))
            for name, target, linktype in links:
                entry = tarfile.TarInfo(name)
                entry.type = linktype
                entry.linkname = target
                stream.addfile(entry)
        return path

    def test_portable_install_creates_launcher_and_metadata(self):
        archive = self.archive("Widget-1.2.tar.gz", [("Widget/bin/widget", self.program, 0o755)])
        info = inspect_archive(archive)
        self.assertEqual(info.kind, "portable")
        self.assertEqual(info.binaries, ("Widget/bin/widget",))
        destination = install_portable(info, info.binaries[0], self.root / "apps", self.root / "launchers")
        executable = destination / "payload/Widget/bin/widget"
        self.assertEqual(executable.read_bytes(), self.program)
        self.assertTrue(executable.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(json.loads((destination / "smarttar.json").read_text())["entry"], "Widget/bin/widget")
        launcher = (self.root / "launchers/smarttar-widget-1.2.desktop").read_text()
        self.assertIn("Type=Application", launcher)
        self.assertIn(str(executable), launcher)

    def test_duplicate_install_refuses_to_replace_existing_files(self):
        archive = self.archive(files=[("widget", self.program, 0o755)])
        info = inspect_archive(archive)
        install_portable(info, "widget", self.root / "apps", self.root / "launchers")
        with self.assertRaises(SmartTarError):
            install_portable(info, "widget", self.root / "apps", self.root / "launchers")

    def test_safe_extraction(self):
        archive = self.archive(files=[("folder/readme.txt", "hello", 0o644)])
        info = inspect_archive(archive)
        destination = self.root / "output"
        unpack_checked(info, destination)
        self.assertEqual((destination / "folder/readme.txt").read_text(), "hello")
        self.assertEqual((destination / "folder/readme.txt").stat().st_mode & 0o777, 0o644)

    def test_parent_traversal_is_rejected(self):
        archive = self.archive(files=[("../outside.txt", "escape", 0o644)])
        with self.assertRaisesRegex(SmartTarError, "traversal"):
            inspect_archive(archive)

    def test_absolute_path_is_rejected(self):
        archive = self.archive(files=[("/tmp/outside.txt", "escape", 0o644)])
        with self.assertRaisesRegex(SmartTarError, "Unsafe archive path"):
            inspect_archive(archive)

    def test_absolute_symbolic_link_is_rejected(self):
        archive = self.archive(links=[("bad", "/etc/passwd", tarfile.SYMTYPE)])
        with self.assertRaisesRegex(SmartTarError, "Unsafe archive link target"):
            inspect_archive(archive)

    def test_dangling_hard_link_is_rejected(self):
        archive = self.archive(links=[("bad", "file", tarfile.LNKTYPE)])
        with self.assertRaisesRegex(SmartTarError, "Dangling archive link target"):
            inspect_archive(archive)

    def test_duplicate_paths_are_rejected(self):
        archive = self.archive(files=[("one.txt", "first", 0o644), ("one.txt", "second", 0o644)])
        with self.assertRaisesRegex(SmartTarError, "Duplicate"):
            inspect_archive(archive)

    def test_file_used_as_directory_is_rejected(self):
        archive = self.archive(files=[("folder", "file", 0o644), ("folder/nested", "file", 0o644)])
        with self.assertRaisesRegex(SmartTarError, "used as a directory"):
            inspect_archive(archive)

    def test_changed_archive_rejected_at_extraction(self):
        archive = self.archive(files=[("good.txt", "one", 0o644)])
        info = inspect_archive(archive)
        self.archive(files=[("new.txt", "two", 0o644)])
        with self.assertRaisesRegex(SmartTarError, "changed"):
            unpack_checked(info, self.root / "destination")
        self.assertFalse((self.root / "destination").exists())

    def test_script_is_inspection_only(self):
        archive = self.archive(files=[("install.sh", "touch /tmp/never-run", 0o755)])
        info = inspect_archive(archive)
        self.assertEqual(info.kind, "install-script")
        self.assertEqual(info.binaries, ())

    def test_pkgbuild_classification(self):
        archive = self.archive(files=[("repo/PKGBUILD", "pkgname=test", 0o644)])
        self.assertEqual(inspect_archive(archive).kind, "pkgbuild")

    def test_arch_package_classification(self):
        archive = self.archive("example.pkg.tar.gz", files=[(".PKGINFO", "pkgname = example", 0o644)])
        self.assertEqual(inspect_archive(archive).kind, "arch-package")

    def test_executable_libraries_do_not_count_as_app(self):
        archive = self.archive(files=[("lib/libexample.so", b"\x7fELFshared", 0o755)])
        self.assertEqual(inspect_archive(archive).kind, "files")

    def test_desktop_exec_escaping(self):
        quoted = desktop_quote('/tmp/My App/$cash`tick"100%.AppImage')
        self.assertEqual(quoted, '"/tmp/My App/\\\\$cash\\\\`tick\\\\"100%%.AppImage"')

    def test_desktop_exec_rejects_control_characters(self):
        with self.assertRaises(SmartTarError):
            desktop_quote("/tmp/one\nExec=unexpected")

    def test_slug_contains_no_desktop_injection_characters(self):
        self.assertEqual(slug_from_archive(Path("a\nExec=evil.tar.gz")), "a-exec-evil")

    def test_setup_and_restoration_preserves_previous_handler(self):
        xdg_data = self.root / "local-data"
        xdg_config = self.root / "local-config"
        xdg_config.mkdir()
        (xdg_config / "mimeapps.list").write_text("# Keep this comment\n[Default Applications]\ntext/plain=org.kde.kate.desktop;\n")
        (xdg_config / "kde-mimeapps.list").write_text("[Default Applications]\napplication/x-compressed-tar=org.kde.ark.desktop;\n")

        def fake_run(*args):
            if args[:3] == ("xdg-mime", "query", "default"):
                line = default_line(xdg_config / "kde-mimeapps.list") or default_line(xdg_config / "mimeapps.list")
                current = line.partition("=")[2].strip().split(";")[0] if line else "org.kde.ark.desktop"
                return subprocess.CompletedProcess(args, 0, current + "\n", "")
            if args[:1] == ("kbuildsycoca6",):
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(args)

        with patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg_data), "XDG_CONFIG_HOME": str(xdg_config), "XDG_CURRENT_DESKTOP": "KDE:DEsk"}):
            with patch("smarttar.shutil.which", return_value="/usr/bin/mock"):
                with patch("smarttar.run_command", side_effect=fake_run):
                    self.assertTrue(configure_association(self.root / "SmartTar Path/smarttar.py"))
                    self.assertIn("application/x-compressed-tar=smarttar-handler.desktop;", (xdg_config / "kde-mimeapps.list").read_text())
                    self.assertIn("application/x-compressed-tar=smarttar-handler.desktop;", (xdg_config / "desk-mimeapps.list").read_text())
                    desktop = (xdg_data / "applications/smarttar-handler.desktop").read_text()
                    self.assertIn('Exec=python3 ', desktop)
                    self.assertIn('"' + str(self.root / "SmartTar Path/smarttar.py") + '"', desktop)
                    self.assertIn("MimeType=application/x-compressed-tar;", desktop)
                    saved = json.loads((xdg_config / "smarttar/association.json").read_text())
                    self.assertEqual(saved["previous_default"], "org.kde.ark.desktop")
                    restore_association()
                    self.assertIn("application/x-compressed-tar=org.kde.ark.desktop;", (xdg_config / "kde-mimeapps.list").read_text())
                    self.assertIn("# Keep this comment", (xdg_config / "mimeapps.list").read_text())
                    self.assertIn("text/plain=org.kde.kate.desktop;", (xdg_config / "mimeapps.list").read_text())
                    self.assertFalse((xdg_config / "desk-mimeapps.list").exists())
                    self.assertFalse((xdg_data / "applications/smarttar-handler.desktop").exists())

    def test_legacy_failed_setup_keeps_original_handler(self):
        xdg_data = self.root / "local-data"
        xdg_config = self.root / "local-config"
        (xdg_config / "smarttar").mkdir(parents=True)
        (xdg_config / "smarttar/association.json").write_text('{"previous_default": "org.kde.ark.desktop"}')
        (xdg_config / "mimeapps.list").write_text("[Default Applications]\napplication/x-compressed-tar=smarttar-handler.desktop;\n")

        def fake_run(*args):
            if args[:3] == ("xdg-mime", "query", "default"):
                return subprocess.CompletedProcess(args, 0, "smarttar-handler.desktop\n", "")
            if args[:1] == ("kbuildsycoca6",):
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(args)

        with patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg_data), "XDG_CONFIG_HOME": str(xdg_config), "XDG_CURRENT_DESKTOP": "KDE"}):
            with patch("smarttar.shutil.which", return_value="/usr/bin/mock"):
                with patch("smarttar.run_command", side_effect=fake_run):
                    configure_association(self.root / "smarttar.py")
                    restore_association()
        restored = (xdg_config / "mimeapps.list").read_text()
        self.assertIn("application/x-compressed-tar=org.kde.ark.desktop;", restored)

    def test_removal_does_not_override_a_later_user_change(self):
        xdg_data = self.root / "local-data"
        xdg_config = self.root / "local-config"
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg_data), "XDG_CONFIG_HOME": str(xdg_config), "XDG_CURRENT_DESKTOP": "KDE"}):
            with patch("smarttar.shutil.which", return_value="/usr/bin/mock"):
                with patch("smarttar.run_command", return_value=subprocess.CompletedProcess([], 0, "org.kde.ark.desktop\n", "")):
                    configure_association(self.root / "smarttar.py")
                    write_default_line(xdg_config / "kde-mimeapps.list", "application/x-compressed-tar=some-other.desktop;\n")
                    restore_association()
        self.assertIn("some-other.desktop;", (xdg_config / "kde-mimeapps.list").read_text())


if __name__ == "__main__":
    unittest.main()
