"""SmartTar read-only diagnostic tests v0.2.0."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import smarttar


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.script = self.data / "smarttar/smarttar.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("# SmartTar test fixture v0.2.0\n")
        self.handler = self.data / "applications" / smarttar.DESKTOP_ID
        smarttar.write_desktop_file(
            self.handler,
            "SmartTar",
            f"python3 {smarttar.desktop_quote(str(self.script))} %f",
            hidden=True,
        )
        self.environment = {
            "XDG_DATA_HOME": str(self.data),
            "XDG_CURRENT_DESKTOP": "KDE:DEsk",
            "DISPLAY": ":test-display",
            "WAYLAND_DISPLAY": "",
        }

    def snapshot(self):
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}

    def test_doctor_passes_without_writing_or_starting_gui(self):
        before = self.snapshot()
        output = io.StringIO()
        query = subprocess.CompletedProcess([], 0, smarttar.DESKTOP_ID + "\n", "")
        with patch.dict(os.environ, self.environment):
            with patch("smarttar.shutil.which", side_effect=lambda name: "/usr/bin/" + name):
                with patch("smarttar.run_command", return_value=query) as command:
                    with redirect_stdout(output):
                        result = smarttar.doctor()
        self.assertEqual(result, 0)
        self.assertIn("Result: PASS", output.getvalue())
        self.assertIn("GUI rendering: not verified", output.getvalue())
        self.assertIn("Handler Exec: valid", output.getvalue())
        command.assert_called_once_with("xdg-mime", "query", "default", smarttar.MIME_TYPE)
        self.assertEqual(self.snapshot(), before)

    def test_doctor_fails_missing_dependency_display_and_default(self):
        output = io.StringIO()
        self.environment.update({"DISPLAY": "", "WAYLAND_DISPLAY": ""})
        query = subprocess.CompletedProcess([], 0, "org.kde.ark.desktop\n", "")
        with patch.dict(os.environ, self.environment):
            with patch("smarttar.shutil.which", side_effect=lambda name: None if name == "kdialog" else "/usr/bin/" + name):
                with patch("smarttar.run_command", return_value=query):
                    with redirect_stdout(output):
                        result = smarttar.doctor()
        self.assertEqual(result, 1)
        self.assertIn("kdialog: MISSING", output.getvalue())
        self.assertIn("Display session environment: MISSING", output.getvalue())
        self.assertIn("Result: FAIL", output.getvalue())

    def test_doctor_rejects_broken_exec_even_when_query_matches(self):
        self.handler.write_text("[Desktop Entry]\nType=Application\nMimeType=application/x-compressed-tar;\nExec=broken-command %f\n")
        output = io.StringIO()
        query = subprocess.CompletedProcess([], 0, smarttar.DESKTOP_ID + "\n", "")
        with patch.dict(os.environ, self.environment):
            with patch("smarttar.shutil.which", return_value="/usr/bin/mock"):
                with patch("smarttar.run_command", return_value=query):
                    with redirect_stdout(output):
                        result = smarttar.doctor()
        self.assertEqual(result, 1)
        self.assertIn("Handler Exec: INVALID", output.getvalue())

    def test_doctor_main_precedes_setup_and_never_mutates(self):
        with patch.object(sys, "argv", ["smarttar.py", "--doctor", "--setup"]):
            with patch("smarttar.doctor", return_value=1) as diagnostic:
                with patch("smarttar.configure_association") as configure:
                    self.assertEqual(smarttar.main(), 1)
        diagnostic.assert_called_once_with()
        configure.assert_not_called()

    def test_setup_unconfirmed_returns_two(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["smarttar.py", "--setup"]):
            with patch("smarttar.configure_association", return_value=False):
                with redirect_stdout(output):
                    result = smarttar.main()
        self.assertEqual(result, 2)
        self.assertIn("activation is not confirmed", output.getvalue())
        self.assertIn("--doctor", output.getvalue())
        self.assertNotIn("now the default handler", output.getvalue())


if __name__ == "__main__":
    unittest.main()
