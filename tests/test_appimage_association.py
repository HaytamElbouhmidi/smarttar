"""SmartTar v0.3.0 independent AppImage MIME upgrade and rollback tests."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import smarttar


class AppImageAssociationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-appimage-mime-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.config = self.root / "config"
        self.config.mkdir()
        self.start_patch(patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_CURRENT_DESKTOP": "KDE:DEsk",
        }))
        self.start_patch(patch.object(smarttar.shutil, "which", return_value="/test/mock"))
        self.start_patch(patch.object(smarttar, "run_command", side_effect=self.command))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def command(self, *args):
        if args[:3] == ("xdg-mime", "query", "default"):
            mime_type = args[3]
            current = ""
            for name in ("kde-mimeapps.list", "mimeapps.list"):
                line = smarttar.default_line(self.config / name, mime_type)
                if line:
                    current = line.partition("=")[2].strip().split(";")[0]
                    break
            return subprocess.CompletedProcess(args, 0, current + "\n", "")
        if args[0] in ("kbuildsycoca6", "update-desktop-database", "update-mime-database"):
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"Unexpected association command: {args!r}")

    def initial_preferences(self):
        original = (
            "# Existing choices\n[Default Applications]\n"
            "text/plain=org.kde.kate.desktop;\n"
            f"{smarttar.MIME_TYPE}=org.kde.ark.desktop;\n"
            "application/vnd.appimage=appimagelauncher.desktop;\n"
            "application/x-iso9660-appimage=legacy-launcher.desktop;\n"
            "application/x-appimage=custom-launcher.desktop;fallback.desktop;\n"
        )
        (self.config / "mimeapps.list").write_text(original)
        return original

    def configure(self):
        return smarttar.configure_association(self.root / "SmartTar path/smarttar.py")

    def test_handler_advertises_only_specific_archive_and_appimage_types(self):
        self.initial_preferences()
        self.assertTrue(self.configure())
        handler = (self.data / "applications" / smarttar.DESKTOP_ID).read_text()
        mime_line = next(line for line in handler.splitlines() if line.startswith("MimeType="))
        declared = set(mime_line.partition("=")[2].split(";")) - {""}
        self.assertEqual(declared, {smarttar.MIME_TYPE, "application/vnd.appimage", "application/x-iso9660-appimage", "application/x-appimage"})
        self.assertNotIn("application/x-executable", handler)
        self.assertNotIn("application/octet-stream", handler)
        self.assertNotIn("application/x-sharedlib", handler)

    def test_each_desktop_gets_each_specific_association(self):
        self.initial_preferences()
        self.configure()
        for filename in ("mimeapps.list", "kde-mimeapps.list", "desk-mimeapps.list"):
            for mime_type in smarttar.MIME_TYPES:
                with self.subTest(filename=filename, mime_type=mime_type):
                    self.assertEqual(smarttar.default_line(self.config / filename, mime_type), f"{mime_type}={smarttar.DESKTOP_ID};\n")

    def test_restore_preserves_all_original_handlers_and_unrelated_preferences(self):
        original = self.initial_preferences()
        self.configure()
        smarttar.restore_association()
        self.assertEqual((self.config / "mimeapps.list").read_text(), original)
        self.assertFalse((self.config / "kde-mimeapps.list").exists())
        self.assertFalse((self.config / "desk-mimeapps.list").exists())

    def test_reinstall_retains_original_appimage_rollback_snapshot(self):
        original = self.initial_preferences()
        self.configure()
        self.configure()
        smarttar.restore_association()
        self.assertEqual((self.config / "mimeapps.list").read_text(), original)

    def test_upgrade_from_tar_only_snapshot_preserves_old_tar_and_appimage_choices(self):
        original = self.initial_preferences()
        mimeapps = self.config / "mimeapps.list"
        smarttar.write_default_line(mimeapps, f"{smarttar.MIME_TYPE}={smarttar.DESKTOP_ID};\n")
        settings = self.config / "smarttar/association.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({
            "previous_default": "org.kde.ark.desktop",
            "overrides": {
                "mimeapps.list": {
                    "previous_line": f"{smarttar.MIME_TYPE}=org.kde.ark.desktop;\n",
                    "file_existed": True,
                },
            },
        }))
        self.configure()
        smarttar.restore_association()
        self.assertEqual(mimeapps.read_text(), original)

    def test_later_user_appimage_choice_is_not_overwritten_on_uninstall(self):
        self.initial_preferences()
        self.configure()
        mimeapps = self.config / "kde-mimeapps.list"
        smarttar.write_default_line(mimeapps, "application/vnd.appimage=user-choice.desktop;\n", "application/vnd.appimage")
        smarttar.restore_association()
        self.assertEqual(smarttar.default_line(mimeapps, "application/vnd.appimage"), "application/vnd.appimage=user-choice.desktop;\n")

    def test_later_appimage_fallback_is_retained_on_uninstall(self):
        self.initial_preferences()
        self.configure()
        mimeapps = self.config / "desk-mimeapps.list"
        smarttar.write_default_line(mimeapps, f"application/vnd.appimage={smarttar.DESKTOP_ID};later-choice.desktop;\n", "application/vnd.appimage")
        smarttar.restore_association()
        self.assertEqual(smarttar.default_line(mimeapps, "application/vnd.appimage"), "application/vnd.appimage=later-choice.desktop;\n")

    def test_partial_activation_is_reported_instead_of_claiming_success(self):
        self.initial_preferences()
        real_command = self.command

        def unresolved(*args):
            result = real_command(*args)
            if args == ("xdg-mime", "query", "default", "application/vnd.appimage"):
                return subprocess.CompletedProcess(args, 0, "other.desktop\n", "")
            return result

        with patch.object(smarttar, "run_command", side_effect=unresolved):
            self.assertFalse(self.configure())


@unittest.skipUnless(shutil.which("xdg-mime"), "The real xdg-mime resolver is required")
class AppImageXdgTests(unittest.TestCase):
    def test_real_resolver_selects_and_restores_all_supported_types(self):
        with tempfile.TemporaryDirectory(prefix="smarttar-appimage-xdg-") as temporary:
            root = Path(temporary)
            config = root / "config"
            data = root / "data"
            apps = data / "applications"
            config.mkdir()
            apps.mkdir(parents=True)
            previous = "previous-appimage-handler.desktop"
            executable = shutil.which("true")
            self.assertIsNotNone(executable)
            (apps / previous).write_text(
                "[Desktop Entry]\nType=Application\nName=Previous test handler\n"
                f"Exec={executable} %f\nMimeType={';'.join(smarttar.MIME_TYPES)};\n"
            )
            original = "[Default Applications]\n" + "".join(f"{mime_type}={previous};\n" for mime_type in smarttar.MIME_TYPES)
            (config / "mimeapps.list").write_text(original)
            environment = {
                "XDG_CONFIG_HOME": str(config),
                "XDG_DATA_HOME": str(data),
                "XDG_CONFIG_DIRS": str(root / "system-config"),
                "XDG_DATA_DIRS": str(root / "system-data"),
                "XDG_CURRENT_DESKTOP": "DEsk",
                "DESKTOP_SESSION": "desk",
                "KDE_FULL_SESSION": "",
                "KDE_SESSION_VERSION": "",
                "GNOME_DESKTOP_SESSION_ID": "",
            }
            real_which = shutil.which

            def dependencies(command, *args, **kwargs):
                if command == "kdialog":
                    return "/test/unused-kdialog"
                if command == "kbuildsycoca6":
                    return None
                return real_which(command, *args, **kwargs)

            def query(mime_type):
                result = subprocess.run(
                    ["xdg-mime", "query", "default", mime_type],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            with patch.dict(os.environ, environment):
                with patch.object(smarttar.shutil, "which", side_effect=dependencies):
                    for mime_type in smarttar.MIME_TYPES:
                        self.assertEqual(query(mime_type), previous)
                    self.assertTrue(smarttar.configure_association(Path(smarttar.__file__).resolve()))
                    for mime_type in smarttar.MIME_TYPES:
                        self.assertEqual(query(mime_type), smarttar.DESKTOP_ID)
                    smarttar.restore_association()
                    for mime_type in smarttar.MIME_TYPES:
                        self.assertEqual(query(mime_type), previous)
            self.assertEqual((config / "mimeapps.list").read_text(), original)


if __name__ == "__main__":
    unittest.main()
