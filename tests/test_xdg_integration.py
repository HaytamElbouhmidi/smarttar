"""SmartTar v0.2.0 integration checks against the real xdg-mime resolver."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import smarttar


@unittest.skipUnless(shutil.which("xdg-mime"), "xdg-mime is not available")
class XdgIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-xdg-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config"
        self.data = self.root / "data"
        self.apps = self.data / "applications"
        self.apps.mkdir(parents=True)
        self.config.mkdir()
        self.old_desktop = "org.kde.ark.desktop"
        self._register_app(self.old_desktop, preference=5)
        self._register_app("custom-archive.desktop", preference=1)
        self._base_config = (
            "# Existing preferences\n[Default Applications]\n"
            "text/plain=org.kde.kate.desktop;\n"
            f"{smarttar.MIME_TYPE}={self.old_desktop};\n"
        )
        (self.config / "mimeapps.list").write_text(self._base_config, encoding="utf-8")
        environment = {
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_DIRS": str(self.root / "system-config"),
            "XDG_DATA_DIRS": str(self.root / "system-data"),
            "XDG_CURRENT_DESKTOP": "DEsk",
            "DESKTOP_SESSION": "desk",
            "KDE_FULL_SESSION": "",
            "KDE_SESSION_VERSION": "",
            "GNOME_DESKTOP_SESSION_ID": "",
            "XDG_UTILS_DEBUG_LEVEL": "2",
        }
        self.environment = patch.dict(os.environ, environment)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        original_which = shutil.which

        def dependencies(command, *args, **kwargs):
            if command == "kdialog":
                return "/test/unused-kdialog"
            if command == "kbuildsycoca6":
                return None
            return original_which(command, *args, **kwargs)

        self.dependencies = patch.object(smarttar.shutil, "which", side_effect=dependencies)
        self.dependencies.start()
        self.addCleanup(self.dependencies.stop)

    def _register_app(self, desktop_id, preference):
        executable = shutil.which("true")
        self.assertIsNotNone(executable)
        (self.apps / desktop_id).write_text(
            "[Desktop Entry]\nType=Application\nName=Archive test fixture\n"
            f"Exec={executable} %f\nMimeType={smarttar.MIME_TYPE};\nInitialPreference={preference}\n",
            encoding="utf-8",
        )

    def query_default(self):
        result = subprocess.run(
            ["xdg-mime", "query", "default", smarttar.MIME_TYPE],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def configure(self):
        return smarttar.configure_association(Path(smarttar.__file__).resolve())

    def test_real_resolver_prefers_handler_over_ark(self):
        self.assertEqual(self.query_default(), self.old_desktop)
        self.configure()
        self.assertEqual(self.query_default(), smarttar.DESKTOP_ID)

    def test_real_resolver_restores_previous_default_and_unrelated_edits(self):
        self.configure()
        self.assertEqual(self.query_default(), smarttar.DESKTOP_ID)
        mimeapps = self.config / "mimeapps.list"
        with mimeapps.open("a", encoding="utf-8") as stream:
            stream.write("\n# Added after SmartTar installation\n[Added Associations]\nimage/png=local-image.desktop;\n")
        smarttar.restore_association()
        self.assertEqual(self.query_default(), self.old_desktop)
        restored = mimeapps.read_text(encoding="utf-8")
        self.assertIn("# Existing preferences", restored)
        self.assertIn("text/plain=org.kde.kate.desktop;", restored)
        self.assertIn("# Added after SmartTar installation", restored)
        self.assertIn("image/png=local-image.desktop;", restored)

    def test_reinstall_handles_a_new_desktop_session(self):
        self.configure()
        os.environ["XDG_CURRENT_DESKTOP"] = "OtherDesk"
        os.environ["DESKTOP_SESSION"] = "otherdesk"
        other_config = self.config / "otherdesk-mimeapps.list"
        original = f"[Default Applications]\n{smarttar.MIME_TYPE}={self.old_desktop};\n"
        other_config.write_text(original, encoding="utf-8")
        self.assertEqual(self.query_default(), self.old_desktop)
        self.configure()
        self.assertEqual(self.query_default(), smarttar.DESKTOP_ID)
        smarttar.restore_association()
        self.assertEqual(other_config.read_text(encoding="utf-8"), original)

    def test_uninstall_preserves_later_explicit_handler_selection(self):
        self.configure()
        smarttar.write_default_line(
            self.config / "desk-mimeapps.list",
            f"{smarttar.MIME_TYPE}=custom-archive.desktop;\n",
        )
        self.assertEqual(self.query_default(), "custom-archive.desktop")
        smarttar.restore_association()
        self.assertEqual(self.query_default(), "custom-archive.desktop")

    def test_reinstall_does_not_replace_original_rollback_snapshot(self):
        self.configure()
        self.configure()
        smarttar.restore_association()
        self.assertEqual(self.query_default(), self.old_desktop)

    def test_restore_retains_lines_appended_after_unterminated_original(self):
        mimeapps = self.config / "mimeapps.list"
        mimeapps.write_text(
            f"[Default Applications]\n{smarttar.MIME_TYPE}={self.old_desktop};",
            encoding="utf-8",
        )
        self.configure()
        with mimeapps.open("a", encoding="utf-8") as stream:
            stream.write("image/png=local-image.desktop;\n")
        smarttar.restore_association()
        lines = mimeapps.read_text(encoding="utf-8").splitlines()
        self.assertIn("image/png=local-image.desktop;", lines)
        self.assertIn(f"{smarttar.MIME_TYPE}={self.old_desktop};", lines)

    def test_uninstall_keeps_a_later_added_fallback(self):
        self.configure()
        smarttar.write_default_line(
            self.config / "desk-mimeapps.list",
            f"{smarttar.MIME_TYPE}={smarttar.DESKTOP_ID};custom-archive.desktop;\n",
        )
        smarttar.restore_association()
        self.assertEqual(self.query_default(), "custom-archive.desktop")


if __name__ == "__main__":
    unittest.main()
