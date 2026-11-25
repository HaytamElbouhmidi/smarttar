"""SmartTar v0.4.0 package migration and installation-mode checks."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import smarttar


PROJECT = Path(__file__).resolve().parents[1]


class SystemInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-system-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.config = self.root / "config"
        self.legacy = self.data / "smarttar/smarttar.py"
        self.handler = self.data / "applications" / smarttar.DESKTOP_ID
        self.settings = self.config / "smarttar/association.json"
        self.system_script = self.root / "system/lib/smarttar/smarttar.py"
        self.system_command = self.root / "system/bin/smarttar"
        self.system_desktop = self.root / "system/share/applications" / smarttar.DESKTOP_ID
        for path in (self.data, self.config, self.system_script.parent, self.system_command.parent):
            path.mkdir(parents=True, exist_ok=True)
        self.system_script.write_text("# SmartTar v0.4.0 package fixture\n")
        self.system_command.write_text("#!/bin/sh\nexit 0\n")
        self.system_command.chmod(0o755)
        smarttar.write_desktop_file(self.system_desktop, "SmartTar", f"{self.system_command} %f", hidden=True)
        self.start_patch(patch.object(smarttar, "SYSTEM_SCRIPT", self.system_script))
        self.start_patch(patch.object(smarttar, "SYSTEM_COMMAND", self.system_command))
        self.start_patch(patch.object(smarttar, "SYSTEM_DESKTOP", self.system_desktop))
        self.start_patch(patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_CURRENT_DESKTOP": "KDE:DEsk",
            "DISPLAY": ":smarttar-test",
            "WAYLAND_DISPLAY": "",
        }))
        self.start_patch(patch.object(smarttar, "require_desktop_user"))
        self.start_patch(patch.object(smarttar.shutil, "which", return_value="/test/dependency"))
        self.commands = self.start_patch(patch.object(smarttar, "run_command", side_effect=self.run_command))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def run_command(self, *args):
        if args[:3] == ("xdg-mime", "query", "default"):
            result = ""
            for name in ("kde-mimeapps.list", "mimeapps.list"):
                line = smarttar.default_line(self.config / name, args[3])
                if line:
                    result = line.partition("=")[2].strip().split(";")[0]
                    break
            return subprocess.CompletedProcess(args, 0, result + "\n", "")
        if args[0] == "kbuildsycoca6":
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"Unexpected command: {args!r}")

    def make_legacy(self):
        self.legacy.parent.mkdir(parents=True, exist_ok=True)
        self.legacy.write_bytes(b"#!/usr/bin/env python3\n# SmartTar v0.3.0 legacy fixture\n")
        self.legacy.chmod(0o755)
        smarttar.write_desktop_file(
            self.handler, "SmartTar", f"python3 {smarttar.desktop_quote(str(self.legacy))} %f", hidden=True,
        )
        previous = "[Default Applications]\ntext/plain=editor.desktop;\n"
        records = {}
        for mime_type in smarttar.MIME_TYPES:
            records[mime_type] = {
                "previous_default": "previous-handler.desktop",
                "overrides": {"mimeapps.list": {
                    "previous_line": f"{mime_type}=previous-handler.desktop;\n",
                    "file_existed": True,
                }},
            }
            previous += f"{mime_type}={smarttar.DESKTOP_ID};\n"
        (self.config / "mimeapps.list").write_text(previous)
        saved = records[smarttar.MIME_TYPE]
        saved["appimage"] = {mime_type: records[mime_type] for mime_type in smarttar.APPIMAGE_MIME_TYPES}
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps(saved, indent=4) + "\n")
        self.payload = self.root / "installed-app/payload/application"
        self.payload.parent.mkdir(parents=True)
        self.payload.write_bytes(b"Installed app stays in place.\n")
        self.app_shortcut = self.data / "applications/smarttar-existing-app.desktop"
        self.app_shortcut.write_bytes(b"[Desktop Entry]\nName=Existing app\nExec=/example/program\n")

    def migrate(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = smarttar.migrate_to_package()
        return result, output.getvalue()

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()
        }

    def backups(self):
        return list((self.legacy.parent / "legacy-backups").glob("*/smarttar.py"))

    def test_legacy_migration_preserves_settings_defaults_apps_and_shortcuts(self):
        self.make_legacy()
        original_script = self.legacy.read_bytes()
        original_settings = self.settings.read_bytes()
        original_defaults = (self.config / "mimeapps.list").read_bytes()
        payload = self.payload.read_bytes()
        shortcut = self.app_shortcut.read_bytes()
        self.assertTrue(self.migrate()[0])
        self.assertFalse(self.legacy.exists())
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_bytes(), original_script)
        self.assertEqual(self.backups()[0].stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.settings.read_bytes(), original_settings)
        self.assertEqual((self.config / "mimeapps.list").read_bytes(), original_defaults)
        self.assertEqual(self.payload.read_bytes(), payload)
        self.assertEqual(self.app_shortcut.read_bytes(), shortcut)
        self.assertEqual(smarttar.desktop_fields(self.handler)[0]["Exec"], f"{self.system_command} %f")

    def test_later_default_and_fallback_choices_are_preserved_exactly(self):
        self.make_legacy()
        preferences = self.config / "kde-mimeapps.list"
        preferences.write_text(
            "# Later explicit choices\n[Default Applications]\n"
            f"{smarttar.MIME_TYPE}=other.desktop;{smarttar.DESKTOP_ID};\n"
            "application/vnd.appimage=custom-appimage.desktop;\n",
        )
        expected = preferences.read_bytes()
        self.migrate()
        self.assertEqual(preferences.read_bytes(), expected)
        self.assertEqual(self.commands.call_args_list, [unittest.mock.call("kbuildsycoca6", "--noincremental")])

    def test_repeat_migration_does_not_create_another_backup(self):
        self.make_legacy()
        self.migrate()
        before = self.snapshot()
        self.migrate()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.backups()), 1)

    def test_fresh_account_delegates_to_setup(self):
        with patch.object(smarttar, "configure_association", return_value=True) as configure:
            self.assertTrue(smarttar.migrate_to_package())
        configure.assert_called_once_with(self.system_script)

    def test_fresh_unconfirmed_setup_is_not_reported_as_success(self):
        output = io.StringIO()
        with patch.object(smarttar, "configure_association", return_value=False):
            with patch.object(sys, "argv", ["smarttar", "--migrate"]):
                with redirect_stdout(output):
                    result = smarttar.main()
        self.assertEqual(result, 2)
        self.assertIn("activation is not confirmed", output.getvalue())
        self.assertNotIn("now uses the system package", output.getvalue())

    def test_missing_package_fails_before_changing_user_files(self):
        self.make_legacy()
        self.system_script.unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(smarttar.SmartTarError, "Install the SmartTar system package"):
            self.migrate()
        self.assertEqual(self.snapshot(), before)

    def test_nonexecutable_package_command_fails_before_changes(self):
        self.make_legacy()
        self.system_command.chmod(0o644)
        before = self.snapshot()
        with self.assertRaises(smarttar.SmartTarError):
            self.migrate()
        self.assertEqual(self.snapshot(), before)

    def test_custom_handler_is_not_overwritten(self):
        self.make_legacy()
        self.handler.write_text("[Desktop Entry]\nType=Application\nExec=custom-program %f\n")
        before = self.snapshot()
        with self.assertRaisesRegex(smarttar.SmartTarError, "customized"):
            self.migrate()
        self.assertEqual(self.snapshot(), before)

    def test_symlinked_handler_is_not_followed_or_replaced(self):
        self.make_legacy()
        outside = self.root / "outside.desktop"
        self.handler.rename(outside)
        self.handler.symlink_to(outside)
        before = outside.read_bytes()
        with self.assertRaises(smarttar.SmartTarError):
            self.migrate()
        self.assertTrue(self.handler.is_symlink())
        self.assertEqual(outside.read_bytes(), before)
        self.assertTrue(self.legacy.exists())

    def test_symlinked_legacy_program_is_not_moved(self):
        self.make_legacy()
        outside = self.root / "outside.py"
        self.legacy.rename(outside)
        self.legacy.symlink_to(outside)
        before = self.handler.read_bytes()
        with self.assertRaisesRegex(smarttar.SmartTarError, "not a regular file"):
            self.migrate()
        self.assertTrue(self.legacy.is_symlink())
        self.assertEqual(self.handler.read_bytes(), before)

    def test_symlinked_backup_location_is_rejected(self):
        self.make_legacy()
        outside = self.root / "outside-backups"
        outside.mkdir()
        (self.legacy.parent / "legacy-backups").symlink_to(outside, target_is_directory=True)
        before = self.snapshot()
        with self.assertRaisesRegex(smarttar.SmartTarError, "backup location"):
            self.migrate()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_legacy_program_owned_by_another_account_is_rejected(self):
        self.make_legacy()
        before = self.snapshot()
        with patch.object(smarttar.os, "geteuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(smarttar.SmartTarError, "owned by this account"):
                self.migrate()
        self.assertEqual(self.snapshot(), before)

    def test_changed_handler_is_not_overwritten_during_migration(self):
        self.make_legacy()
        original_mkdtemp = smarttar.tempfile.mkdtemp

        def changed_handler(*args, **kwargs):
            result = original_mkdtemp(*args, **kwargs)
            self.handler.write_bytes(b"Changed by another operation.\n")
            return result

        with patch.object(smarttar.tempfile, "mkdtemp", side_effect=changed_handler):
            with self.assertRaisesRegex(smarttar.SmartTarError, "handler changed"):
                self.migrate()
        self.assertEqual(self.handler.read_bytes(), b"Changed by another operation.\n")
        self.assertTrue(self.legacy.exists())
        self.assertEqual(self.backups(), [])

    def test_cache_refresh_failure_is_an_honest_warning_after_migration(self):
        self.make_legacy()
        warnings = io.StringIO()
        with patch.object(smarttar, "run_command", side_effect=smarttar.SmartTarError("Synthetic cache timeout")):
            with redirect_stderr(warnings):
                self.assertTrue(self.migrate()[0])
        self.assertIn("KDE cache refresh failed", warnings.getvalue())
        self.assertFalse(self.legacy.exists())
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(smarttar.desktop_fields(self.handler)[0]["Exec"], f"{self.system_command} %f")

    def test_failed_handler_replacement_preserves_legacy_and_preferences(self):
        self.make_legacy()
        before = self.snapshot()
        with patch.object(smarttar.os, "replace", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])

    def test_failed_legacy_move_restores_original_handler(self):
        self.make_legacy()
        before = self.snapshot()
        with patch.object(Path, "rename", side_effect=OSError("Synthetic move failure")):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])

    def test_failed_legacy_move_removes_only_the_new_handler(self):
        self.make_legacy()
        self.handler.unlink()
        before = self.snapshot()
        with patch.object(Path, "rename", side_effect=OSError("Synthetic move failure")):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.handler.exists())

    def test_rollback_does_not_replace_a_later_handler_change(self):
        self.make_legacy()
        latest = b"Changed independently after migration wrote the handler.\n"

        def failed_move(*args, **kwargs):
            self.handler.write_bytes(latest)
            raise OSError("Synthetic move failure")

        warnings = io.StringIO()
        with patch.object(Path, "rename", side_effect=failed_move):
            with redirect_stderr(warnings):
                with self.assertRaises(OSError):
                    self.migrate()
        self.assertEqual(self.handler.read_bytes(), latest)
        self.assertTrue(self.legacy.exists())
        self.assertIn("latest state was left untouched", warnings.getvalue())

    def test_rollback_does_not_recreate_a_handler_removed_independently(self):
        self.make_legacy()

        def failed_move(*args, **kwargs):
            self.handler.unlink()
            raise OSError("Synthetic move failure")

        with patch.object(Path, "rename", side_effect=failed_move):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(OSError):
                    self.migrate()
        self.assertFalse(self.handler.exists())
        self.assertTrue(self.legacy.exists())

    def test_changed_legacy_file_is_not_moved_and_handler_is_restored(self):
        self.make_legacy()
        handler_before = self.handler.read_bytes()
        original_writer = smarttar.write_desktop_file

        def changed_source(*args, **kwargs):
            original_writer(*args, **kwargs)
            self.legacy.write_bytes(b"Changed by another operation.\n")

        with patch.object(smarttar, "write_desktop_file", side_effect=changed_source):
            with self.assertRaisesRegex(smarttar.SmartTarError, "changed during migration"):
                self.migrate()
        self.assertEqual(self.legacy.read_bytes(), b"Changed by another operation.\n")
        self.assertEqual(self.handler.read_bytes(), handler_before)
        self.assertEqual(self.backups(), [])

    def test_restore_after_migration_uses_the_original_snapshots(self):
        self.make_legacy()
        self.migrate()
        smarttar.restore_association()
        for mime_type in smarttar.MIME_TYPES:
            self.assertEqual(
                smarttar.default_line(self.config / "mimeapps.list", mime_type),
                f"{mime_type}=previous-handler.desktop;\n",
            )
        self.assertFalse(self.handler.exists())
        self.assertFalse(self.settings.exists())
        self.assertTrue(self.system_script.exists())
        self.assertTrue(self.system_desktop.exists())
        self.assertTrue(self.payload.exists())
        self.assertTrue(self.app_shortcut.exists())
        self.assertEqual(len(self.backups()), 1)

    def test_packaged_setup_does_not_point_to_a_downloaded_copy(self):
        self.assertTrue(smarttar.configure_association(self.root / "Downloads/smarttar.py"))
        self.assertEqual(smarttar.desktop_fields(self.handler)[0]["Exec"], f"{self.system_command} %f")
        self.assertFalse(self.legacy.exists())

    def test_package_doctor_passes_using_the_system_handler_without_changes(self):
        (self.config / "mimeapps.list").write_text(
            "[Default Applications]\n" + "".join(f"{mime_type}={smarttar.DESKTOP_ID};\n" for mime_type in smarttar.MIME_TYPES),
        )
        before = self.snapshot()
        output = io.StringIO()
        with redirect_stdout(output):
            result = smarttar.doctor()
        self.assertEqual(result, 0, output.getvalue())
        self.assertIn("Installation mode: system package", output.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_doctor_detects_the_legacy_user_handler_shadowing_the_package(self):
        self.make_legacy()
        before = self.snapshot()
        output = io.StringIO()
        with redirect_stdout(output):
            result = smarttar.doctor()
        self.assertEqual(result, 1)
        self.assertIn("User handler shadows the system package", output.getvalue())
        self.assertIn("Handler Exec: INVALID", output.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_doctor_passes_after_migration_without_starting_the_app(self):
        self.make_legacy()
        self.migrate()
        output = io.StringIO()
        with redirect_stdout(output):
            result = smarttar.doctor()
        self.assertEqual(result, 0, output.getvalue())
        self.assertIn("AppImage launch compatibility: not verified", output.getvalue())


class DesktopUserGuardTests(unittest.TestCase):
    def test_root_setup_migration_and_restore_are_rejected(self):
        with patch.object(smarttar.os, "geteuid", return_value=0):
            for operation in (
                lambda: smarttar.configure_association(Path("/unused.py")),
                smarttar.migrate_to_package,
                smarttar.restore_association,
            ):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(smarttar.SmartTarError, "without sudo"):
                        operation()

    def test_sudo_environment_is_rejected_even_with_a_nonroot_uid(self):
        with patch.object(smarttar.os, "geteuid", return_value=1000):
            with patch.dict(os.environ, {"SUDO_USER": "test-user", "SUDO_UID": "1000"}):
                with self.assertRaises(smarttar.SmartTarError):
                    smarttar.require_desktop_user()

    def test_read_only_doctor_is_allowed_as_root(self):
        with patch.object(smarttar.os, "geteuid", return_value=0):
            with patch.object(sys, "argv", ["smarttar", "--doctor"]):
                with patch.object(smarttar, "doctor", return_value=0) as doctor:
                    self.assertEqual(smarttar.main(), 0)
        doctor.assert_called_once_with()

    def test_root_extract_is_rejected_before_reading_or_writing_downloads(self):
        with patch.object(smarttar.os, "geteuid", return_value=0):
            with patch.object(sys, "argv", ["smarttar", "example.tar.gz", "--extract", "/unused"]):
                with patch.object(smarttar, "inspect_archive") as inspect:
                    with patch.object(smarttar, "unpack_checked") as unpack:
                        with redirect_stderr(io.StringIO()):
                            self.assertEqual(smarttar.main(), 1)
        inspect.assert_not_called()
        unpack.assert_not_called()

    def test_root_graphical_invocation_does_not_start_a_dialog(self):
        with patch.object(smarttar.os, "geteuid", return_value=0):
            with patch.object(sys, "argv", ["smarttar", "example.AppImage"]):
                with patch.object(smarttar, "run_gui") as gui:
                    with patch.object(smarttar, "notify") as notify:
                        with redirect_stderr(io.StringIO()):
                            self.assertEqual(smarttar.main(), 1)
        gui.assert_not_called()
        notify.assert_not_called()


@unittest.skipUnless(shutil.which("bash") and os.geteuid() != 0, "Requires a non-root bash test process")
class PackageShellFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-package-shell-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.data = self.root / "data"
        self.bin = self.root / "bin"
        self.log = self.root / "calls.log"
        self.system_script = self.root / "system/lib/smarttar/smarttar.py"
        self.system_command = self.root / "system/bin/smarttar"
        for path in (self.project / "tools", self.project / "packaging/keys", self.bin, self.data, self.system_script.parent, self.system_command.parent):
            path.mkdir(parents=True, exist_ok=True)
        (self.project / "tools/configure_repository.py").write_text("# SmartTar v0.4.0 helper fixture, not executed\n")
        (self.project / "packaging/keys/smarttar.asc").write_text("Public-key fixture, not imported.\n")
        for filename in ("install.sh", "uninstall.sh"):
            source = (PROJECT / filename).read_text()
            source = source.replace("/usr/lib/smarttar/smarttar.py", str(self.system_script))
            source = source.replace("/usr/bin/smarttar", str(self.system_command))
            (self.project / filename).write_text(source)
        self.environment = os.environ.copy()
        self.environment.pop("SUDO_USER", None)
        self.environment.pop("SUDO_UID", None)
        self.environment.update({
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "PATH": str(self.bin) + os.pathsep + self.environment.get("PATH", ""),
        })
        self.stub(self.bin / "sudo", f"printf 'helper\\n' >> '{self.log}'\nprintf '%s\\n' \"$@\" >> '{self.log}'\nexit 0\n")
        self.system_script.write_text("# SmartTar v0.4.0 package fixture\n")
        self.stub(self.system_command, f"printf 'command %s\\n' \"$@\" >> '{self.log}'\nexit 0\n")
        self.sentinel = self.data / "keep-existing-app"
        self.sentinel.write_bytes(b"Keep existing applications and user settings.\n")

    def stub(self, path, body):
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def run_script(self, name, *arguments):
        return subprocess.run(["bash", str(self.project / name), *arguments], env=self.environment, capture_output=True, text=True, timeout=10)

    def test_default_installer_calls_privileged_helper_then_user_migration(self):
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.log.read_text().splitlines()
        self.assertEqual(lines, [
            "helper", "/usr/bin/python3", str(self.project / "tools/configure_repository.py"),
            "--key", str(self.project / "packaging/keys/smarttar.asc"), "command --migrate",
        ])
        self.assertFalse((self.data / "smarttar/smarttar.py").exists())
        self.assertTrue(self.sentinel.exists())

    def test_helper_failure_does_not_call_migration_or_change_user_files(self):
        self.stub(self.bin / "sudo", f"printf 'failed-helper\\n' >> '{self.log}'\nexit 27\n")
        before = self.sentinel.read_bytes()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 27)
        self.assertEqual(self.log.read_text(), "failed-helper\n")
        self.assertEqual(self.sentinel.read_bytes(), before)
        self.assertFalse((self.data / "smarttar").exists())

    def test_migration_failure_does_not_print_ready(self):
        self.stub(self.system_command, "exit 2\n")
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("SmartTar is ready", result.stdout)

    def test_missing_package_after_helper_success_does_not_call_migration(self):
        self.system_script.unlink()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("command --migrate", self.log.read_text())
        self.assertIn("Existing user settings were not changed", result.stderr)

    def test_manual_mode_cannot_hide_an_installed_package(self):
        result = self.run_script("install.sh", "--manual")
        self.assertEqual(result.returncode, 1)
        self.assertIn("would hide its updates", result.stderr)
        self.assertFalse((self.data / "smarttar").exists())
        self.assertFalse(self.log.exists())

    def test_sudo_invocation_is_rejected_before_any_commands(self):
        self.environment["SUDO_USER"] = "test-user"
        for script in ("install.sh", "uninstall.sh"):
            with self.subTest(script=script):
                result = self.run_script(script)
                self.assertEqual(result.returncode, 1)
                self.assertIn("without sudo", result.stderr)
        self.assertFalse(self.log.exists())

    def test_package_uninstall_only_restores_this_user_and_prints_system_removal(self):
        result = self.run_script("uninstall.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text(), "command --restore-association\n")
        self.assertIn("sudo pacman -R smarttar", result.stdout)
        self.assertIn("system package is still installed", result.stdout)
        self.assertTrue(self.system_script.exists())
        self.assertTrue(self.system_command.exists())
        self.assertTrue(self.sentinel.exists())

    def test_incomplete_package_uninstall_does_not_modify_user_files(self):
        self.system_command.chmod(0o644)
        result = self.run_script("uninstall.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("system package is incomplete", result.stderr)
        self.assertTrue(self.sentinel.exists())
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
