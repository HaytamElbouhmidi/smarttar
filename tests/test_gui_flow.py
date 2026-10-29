"""SmartTar v0.2.0 dialog-flow tests without a live desktop session."""

import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import smarttar


class ScriptedDialogs:
    def __init__(self, testcase, responses):
        self.testcase = testcase
        self.responses = list(responses)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.testcase.assertEqual(args[0], "kdialog", "Downloaded applications must not execute during installation")
        self.testcase.assertTrue(self.responses, f"Unexpected dialog: {args!r}")
        option, returncode, output = self.responses.pop(0)
        self.testcase.assertIn(option, args, f"Expected {option}, got {args!r}")
        self.calls.append(args)
        error = "KDialog test failure" if returncode not in (0, 1) else ""
        return subprocess.CompletedProcess(args, returncode, output, error)

    def assert_finished(self):
        self.testcase.assertEqual(self.responses, [], "Not all expected dialogs were reached")


@unittest.skipUnless(shutil.which("true"), "A harmless Linux ELF fixture is required")
class GuiFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-gui-flow-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install_root = self.root / "installed"
        self.launchers = self.root / "launchers"
        self.payload = Path(shutil.which("true")).read_bytes()
        self.start_patch(patch.object(smarttar, "app_root", return_value=self.install_root))
        self.start_patch(patch.object(smarttar, "application_dir", return_value=self.launchers))
        original_which = shutil.which

        def dependencies(command, *args, **kwargs):
            if command == "kdialog":
                return "/test/scripted-kdialog"
            if command == "kbuildsycoca6":
                return None
            return original_which(command, *args, **kwargs)

        self.start_patch(patch.object(smarttar.shutil, "which", side_effect=dependencies))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def archive(self, files=None):
        archive = self.root / "sample.tar.gz"
        files = files if files is not None else [("sample/app", self.payload, 0o755)]
        with tarfile.open(archive, "w:gz") as tar:
            for name, contents, mode in files:
                member = tarfile.TarInfo(name)
                member.mode = mode
                member.size = len(contents)
                tar.addfile(member, io.BytesIO(contents))
        return archive

    def dialogs(self, responses):
        dialogs = ScriptedDialogs(self, responses)
        self.start_patch(patch.object(smarttar, "run_command", side_effect=dialogs))
        return dialogs

    def assert_no_install(self):
        self.assertFalse(self.install_root.exists())
        self.assertFalse(self.launchers.exists())

    def test_install_flow_copies_app_and_launcher_without_executing_it(self):
        archive = self.archive()
        dialogs = self.dialogs([
            ("--menu", 0, "install\n"),
            ("--yesno", 0, ""),
            ("--msgbox", 0, ""),
        ])
        smarttar.run_gui(archive)
        dialogs.assert_finished()
        installed = self.install_root / "sample/payload/sample/app"
        self.assertEqual(installed.read_bytes(), self.payload)
        self.assertTrue((self.launchers / "smarttar-sample.desktop").is_file())

    def test_main_cancel_returns_success_without_writes(self):
        archive = self.archive()
        dialogs = self.dialogs([("--menu", 1, "")])
        with patch.object(sys, "argv", ["smarttar.py", str(archive)]):
            self.assertEqual(smarttar.main(), 0)
        dialogs.assert_finished()
        self.assert_no_install()

    def test_declined_install_confirmation_does_not_install(self):
        archive = self.archive()
        dialogs = self.dialogs([
            ("--menu", 0, "install\n"),
            ("--yesno", 1, ""),
            ("--menu", 1, ""),
        ])
        smarttar.run_gui(archive)
        dialogs.assert_finished()
        self.assert_no_install()

    def test_extract_flow_writes_only_the_selected_new_folder(self):
        archive = self.archive([("documents/readme.txt", b"Local fixture", 0o644)])
        destination_parent = self.root / "extraction"
        destination_parent.mkdir()
        dialogs = self.dialogs([
            ("--menu", 0, "extract\n"),
            ("--getexistingdirectory", 0, str(destination_parent) + "\n"),
            ("--yesno", 0, ""),
            ("--msgbox", 0, ""),
        ])
        smarttar.run_gui(archive)
        dialogs.assert_finished()
        self.assertEqual((destination_parent / "sample/documents/readme.txt").read_bytes(), b"Local fixture")
        self.assert_no_install()

    def test_directory_picker_cancel_returns_to_menu(self):
        archive = self.archive([("readme.txt", b"Local fixture", 0o644)])
        dialogs = self.dialogs([
            ("--menu", 0, "extract\n"),
            ("--getexistingdirectory", 1, ""),
            ("--menu", 1, ""),
        ])
        smarttar.run_gui(archive)
        dialogs.assert_finished()
        self.assert_no_install()

    def test_multi_candidate_flow_uses_the_selected_executable(self):
        archive = self.archive([
            ("sample/first", self.payload, 0o755),
            ("sample/second", self.payload, 0o755),
        ])
        dialogs = self.dialogs([
            ("--menu", 0, "install\n"),
            ("--menu", 0, "sample/second\n"),
            ("--yesno", 0, ""),
            ("--msgbox", 0, ""),
        ])
        smarttar.run_gui(archive)
        dialogs.assert_finished()
        metadata = json.loads((self.install_root / "sample/smarttar.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["entry"], "sample/second")
        launcher = (self.launchers / "smarttar-sample.desktop").read_text(encoding="utf-8")
        self.assertIn("sample/second", launcher)

    def test_multi_candidate_cancel_does_not_install(self):
        archive = self.archive([
            ("sample/first", self.payload, 0o755),
            ("sample/second", self.payload, 0o755),
        ])
        dialogs = self.dialogs([
            ("--menu", 0, "install\n"),
            ("--menu", 1, ""),
            ("--menu", 1, ""),
        ])
        smarttar.run_gui(archive)
        dialogs.assert_finished()
        self.assert_no_install()

    def test_dialog_failure_is_not_treated_as_cancellation(self):
        archive = self.archive()
        dialogs = self.dialogs([("--menu", 2, "")])
        with self.assertRaises(smarttar.SmartTarError):
            smarttar.run_gui(archive)
        dialogs.assert_finished()
        self.assert_no_install()

    def test_main_reports_dialog_failure_with_nonzero_status(self):
        archive = self.archive()
        dialogs = self.dialogs([
            ("--menu", 2, ""),
            ("--error", 0, ""),
        ])
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["smarttar.py", str(archive)]), redirect_stderr(stderr):
            self.assertEqual(smarttar.main(), 1)
        dialogs.assert_finished()
        self.assertTrue(stderr.getvalue().strip())
        self.assert_no_install()

    def test_shell_script_archive_has_no_direct_install_action(self):
        archive = self.archive([("install.sh", b"#!/bin/sh\nexit 0\n", 0o755)])
        dialogs = self.dialogs([("--menu", 1, "")])
        smarttar.run_gui(archive)
        dialogs.assert_finished()
        self.assertNotIn("install", dialogs.calls[0])
        self.assert_no_install()


if __name__ == "__main__":
    unittest.main()
