"""SmartTar v0.2.0 user-level installation script integration checks."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("xdg-mime") and shutil.which("bash"), "Requires bash and xdg-mime")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="smarttar-script-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config"
        self.data = self.root / "data"
        self.bin = self.root / "bin"
        for path in (self.config, self.data / "applications", self.bin, self.root / "config-system", self.root / "data-system"):
            path.mkdir(parents=True)
        self.stub("kdialog", "#!/bin/sh\nexit 0\n")
        self.env = os.environ.copy()
        self.env.update({
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_DATA_HOME": str(self.data),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_CONFIG_DIRS": str(self.root / "config-system"),
            "XDG_DATA_DIRS": str(self.root / "data-system"),
            "XDG_CURRENT_DESKTOP": "DEsk",
            "DESKTOP_SESSION": "desk",
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "WAYLAND_DISPLAY": "smarttar-test-only",
        })
        self.env.pop("KDE_FULL_SESSION", None)
        self.env.pop("KDE_SESSION_VERSION", None)
        self.original = "[Default Applications]\napplication/x-compressed-tar=org.kde.ark.desktop;\ntext/plain=editor.desktop;\n"
        (self.config / "mimeapps.list").write_text(self.original, encoding="utf-8")
        (self.data / "applications/org.kde.ark.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Archive fixture\nExec=true %f\nMimeType=application/x-compressed-tar;\n",
            encoding="utf-8",
        )

    def stub(self, name, content):
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run_script(self, name):
        return subprocess.run(["bash", str(PROJECT / name)], env=self.env, capture_output=True, text=True, timeout=30)

    def test_install_doctor_and_uninstall_use_real_xdg_lookup(self):
        installed = self.run_script("install.sh")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        program = self.data / "smarttar/smarttar.py"
        self.assertTrue(program.is_file())
        diagnosis = subprocess.run(["python3", str(program), "--doctor"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(diagnosis.returncode, 0, diagnosis.stdout + diagnosis.stderr)
        self.assertIn("not verified", diagnosis.stdout)
        removed = self.run_script("uninstall.sh")
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertFalse(program.exists())
        self.assertFalse((self.data / "applications/smarttar-handler.desktop").exists())
        self.assertEqual((self.config / "mimeapps.list").read_text(), self.original)

    def test_unconfirmed_setup_is_not_reported_as_success(self):
        self.stub("xdg-mime", "#!/bin/sh\nprintf 'fixture resolver failure\\n' >&2\nexit 1\n")
        installed = self.run_script("install.sh")
        self.assertEqual(installed.returncode, 2, installed.stdout + installed.stderr)
        self.assertNotIn("installed for the current user", installed.stdout)


if __name__ == "__main__":
    unittest.main()
