"""SmartTar v0.3.0 AppImage recognition, installation, and dialog tests."""

from contextlib import redirect_stderr, redirect_stdout
import configparser
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import smarttar


def image_bytes(image_type=2, machine=62, elf_class=2, byte_order=1, payload=b"inert fixture payload"):
    """Build marked ELF test data, not a runnable third-party application."""
    header = bytearray(64 if elf_class == 2 else 52)
    header[:4] = b"\x7fELF"
    header[4:7] = bytes((elf_class, byte_order, 1))
    header[8:11] = b"AI" + bytes((image_type,))
    order = "<" if byte_order == 1 else ">"
    struct.pack_into(order + "HHI", header, 16, 2, machine, 1)
    struct.pack_into(order + "H", header, 52 if elf_class == 2 else 40, len(header))
    return bytes(header) + payload


class TrackedReader:
    def __init__(self, stream, reads, maximum):
        self.stream = stream
        self.reads = reads
        self.maximum = maximum

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def read(self, size=-1):
        if not 0 <= size <= self.maximum:
            raise AssertionError(f"Unexpected unbounded read: {size}")
        data = self.stream.read(size)
        self.reads.append(len(data))
        return data


class AppImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-appimage-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.apps = self.root / "installed"
        self.launchers = self.root / "launchers"
        self.host = patch.object(smarttar.platform, "machine", return_value="x86_64")
        self.host.start()
        self.addCleanup(self.host.stop)

    def fixture(self, name="Widget-1.2.AppImage", **kwargs):
        image = self.root / name
        image.write_bytes(image_bytes(**kwargs))
        image.chmod(0o644)
        return image

    def install(self, image):
        return smarttar.install_appimage(smarttar.inspect_appimage(image), self.apps, self.launchers)

    def assert_unpublished(self, slug="widget-1.2"):
        self.assertFalse((self.apps / slug).exists())
        self.assertFalse((self.launchers / f"smarttar-{slug}.desktop").exists())
        self.assertEqual(list(self.apps.glob(".smarttar-*")), [])
        self.assertEqual(list(self.launchers.glob(".smarttar-desktop-*")), [])

    def test_type_one_and_type_two_are_recognized_without_execution(self):
        for image_type in (1, 2):
            with self.subTest(image_type=image_type):
                image = self.fixture(image_type=image_type)
                with patch.object(smarttar.subprocess, "run") as run:
                    with patch.object(smarttar.subprocess, "Popen") as popen:
                        info = smarttar.inspect_appimage(image)
                self.assertEqual(info.image, image)
                self.assertEqual(info.appimage_type, image_type)
                self.assertEqual(info.architecture, "x86_64")
                self.assertEqual(info.size, image.stat().st_size)
                self.assertEqual(info.as_dict()["kind"], "appimage")
                self.assertEqual(info.as_dict()["version"], smarttar.VERSION)
                run.assert_not_called()
                popen.assert_not_called()

    def test_extension_is_case_insensitive(self):
        for suffix in (".AppImage", ".Appimage", ".appimage", ".APPIMAGE"):
            with self.subTest(suffix=suffix):
                self.assertEqual(smarttar.inspect_appimage(self.fixture("Widget" + suffix)).appimage_type, 2)

    def test_large_sparse_image_inspection_reads_only_the_header(self):
        image = self.fixture()
        with image.open("r+b") as stream:
            stream.truncate(1024 ** 3 + 1)
        reads = []
        real_fdopen = os.fdopen

        def tracked_fdopen(descriptor, mode="r", *args, **kwargs):
            stream = real_fdopen(descriptor, mode, *args, **kwargs)
            return TrackedReader(stream, reads, 64) if mode == "rb" else stream

        with patch.object(smarttar.os, "fdopen", side_effect=tracked_fdopen):
            info = smarttar.inspect_appimage(image)
        self.assertGreater(info.size, 1024 ** 3)
        self.assertTrue(reads)
        self.assertLessEqual(sum(reads), 64)

    def test_native_architectures_are_accepted(self):
        for host, machine, elf_class in (("x86_64", 62, 2), ("aarch64", 183, 2), ("i686", 3, 1), ("armv7l", 40, 1)):
            with self.subTest(host=host):
                image = self.fixture(machine=machine, elf_class=elf_class)
                with patch.object(smarttar.platform, "machine", return_value=host):
                    self.assertTrue(smarttar.inspect_appimage(image).architecture)

    def test_wrong_architecture_is_rejected(self):
        image = self.fixture(machine=183)
        with self.assertRaises(smarttar.SmartTarError):
            smarttar.inspect_appimage(image)

    def test_little_endian_native_header_is_accepted(self):
        image = self.fixture(byte_order=1)
        self.assertEqual(smarttar.inspect_appimage(image).architecture, "x86_64")

    def test_big_endian_header_is_not_misidentified_as_native(self):
        image = self.fixture(byte_order=2)
        with self.assertRaises(smarttar.SmartTarError):
            smarttar.inspect_appimage(image)

    def test_position_independent_elf_runtime_is_accepted(self):
        image = self.fixture()
        data = bytearray(image.read_bytes())
        struct.pack_into("<H", data, 16, 3)
        image.write_bytes(data)
        self.assertEqual(smarttar.inspect_appimage(image).appimage_type, 2)

    def test_other_extensions_are_not_intercepted(self):
        for filename in ("Widget.bin", "Widget", "Widget.exe"):
            with self.subTest(filename=filename):
                with self.assertRaises(smarttar.SmartTarError):
                    smarttar.inspect_appimage(self.fixture(filename))

    def test_mismatched_elf_class_and_machine_are_rejected(self):
        image = self.fixture(machine=62, elf_class=1)
        with self.assertRaises(smarttar.SmartTarError):
            smarttar.inspect_appimage(image)

    def test_unknown_machine_is_rejected(self):
        image = self.fixture(machine=65535)
        with self.assertRaises(smarttar.SmartTarError):
            smarttar.inspect_appimage(image)

    def test_truncated_or_non_appimage_data_are_rejected(self):
        image = self.fixture()
        valid = image.read_bytes()
        variants = [b"", b"\x7fELF", valid[:31], b"#!/bin/sh\nexit 0\n", valid[:8] + b"XYZ" + valid[11:]]
        for data in variants:
            with self.subTest(data=data[:12]):
                image.write_bytes(data)
                with self.assertRaises(smarttar.SmartTarError):
                    smarttar.inspect_appimage(image)

    def test_header_without_a_payload_is_rejected(self):
        for elf_class, machine, host in ((1, 3, "i686"), (2, 62, "x86_64")):
            with self.subTest(elf_class=elf_class):
                image = self.fixture(elf_class=elf_class, machine=machine, payload=b"")
                with patch.object(smarttar.platform, "machine", return_value=host):
                    with self.assertRaises(smarttar.SmartTarError):
                        smarttar.inspect_appimage(image)

    def test_invalid_elf_header_fields_are_rejected(self):
        image = self.fixture()
        valid = image.read_bytes()
        for offset, value in ((4, 0), (5, 0), (6, 0), (10, 3), (16, 1), (20, 0), (52, 0)):
            with self.subTest(offset=offset):
                data = bytearray(valid)
                data[offset] = value
                image.write_bytes(data)
                with self.assertRaises(smarttar.SmartTarError):
                    smarttar.inspect_appimage(image)

    def test_file_limit_is_enforced_before_copy(self):
        image = self.fixture(payload=b"x" * 2048)
        with patch.object(smarttar, "MAX_APPIMAGE_BYTES", 1024):
            with self.assertRaises(smarttar.SmartTarError):
                smarttar.inspect_appimage(image)

    def test_symlinks_and_directories_are_rejected(self):
        actual = self.fixture()
        link = self.root / "Linked.AppImage"
        link.symlink_to(actual)
        directory = self.root / "Directory.AppImage"
        directory.mkdir()
        for image in (link, directory):
            with self.subTest(image=image.name):
                with self.assertRaises((smarttar.SmartTarError, OSError)):
                    smarttar.inspect_appimage(image)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO checks require a Unix filesystem")
    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        image = self.root / "Pipe.AppImage"
        os.mkfifo(image)
        with self.assertRaises((smarttar.SmartTarError, OSError)):
            smarttar.inspect_appimage(image)

    def test_install_copies_and_creates_launcher_without_running_application(self):
        image = self.fixture()
        before = image.read_bytes(), image.stat().st_mode, image.stat().st_mtime_ns
        with patch.object(smarttar.subprocess, "run") as run:
            with patch.object(smarttar.subprocess, "Popen") as popen:
                installed = self.install(image)
        executable = installed / "payload" / image.name
        self.assertEqual(executable.read_bytes(), before[0])
        self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o755)
        self.assertEqual((image.read_bytes(), image.stat().st_mode, image.stat().st_mtime_ns), before)
        launcher = (self.launchers / "smarttar-widget-1.2.desktop").read_text()
        self.assertIn("Exec=" + smarttar.desktop_quote(str(executable)), launcher)
        self.assertIn("Icon=application-x-executable", launcher)
        self.assertNotIn("--appimage-extract", launcher)
        self.assertNotIn("--no-sandbox", launcher)
        metadata = json.loads((installed / "smarttar.json").read_text())
        self.assertEqual(metadata["smarttar_version"], smarttar.VERSION)
        self.assertEqual(metadata["entry"], image.name)
        self.assertIn(hashlib.sha256(before[0]).hexdigest(), metadata.values())
        run.assert_not_called()
        popen.assert_not_called()

    def test_copy_is_bounded_and_does_not_reread_the_full_payload(self):
        image = self.fixture(payload=b"x" * (2 * 1024 * 1024 + 123))
        info = smarttar.inspect_appimage(image)
        reads = []
        real_fdopen = os.fdopen

        def tracked_fdopen(descriptor, mode="r", *args, **kwargs):
            stream = real_fdopen(descriptor, mode, *args, **kwargs)
            return TrackedReader(stream, reads, smarttar.COPY_CHUNK_BYTES) if mode == "rb" else stream

        with patch.object(smarttar.os, "fdopen", side_effect=tracked_fdopen):
            installed = smarttar.install_appimage(info, self.apps, self.launchers)
        self.assertTrue(reads)
        self.assertGreaterEqual(sum(reads), info.size)
        self.assertLessEqual(sum(reads), info.size + 128)
        self.assertEqual((installed / "payload" / image.name).read_bytes(), image.read_bytes())

    def test_source_hardlink_modes_are_preserved(self):
        image = self.fixture()
        other_name = self.root / "same-download.AppImage"
        os.link(image, other_name)
        original_mode = stat.S_IMODE(image.stat().st_mode)
        installed = self.install(image)
        executable = installed / "payload" / image.name
        self.assertEqual(stat.S_IMODE(image.stat().st_mode), original_mode)
        self.assertEqual(stat.S_IMODE(other_name.stat().st_mode), original_mode)
        self.assertTrue(image.samefile(other_name))
        self.assertFalse(image.samefile(executable))
        self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o755)

    def test_duplicate_install_preserves_existing_installation(self):
        image = self.fixture()
        installed = self.install(image)
        marker = installed / "keep.txt"
        marker.write_text("existing installation")
        launcher = self.launchers / "smarttar-widget-1.2.desktop"
        before = launcher.read_bytes()
        with self.assertRaises(smarttar.SmartTarError):
            self.install(image)
        self.assertEqual(marker.read_text(), "existing installation")
        self.assertEqual(launcher.read_bytes(), before)

    def test_existing_launcher_is_not_overwritten(self):
        image = self.fixture()
        self.launchers.mkdir()
        launcher = self.launchers / "smarttar-widget-1.2.desktop"
        launcher.write_text("keep this launcher")
        with self.assertRaises(smarttar.SmartTarError):
            self.install(image)
        self.assertEqual(launcher.read_text(), "keep this launcher")
        self.assertFalse((self.apps / "widget-1.2").exists())

    def test_source_changed_since_inspection_is_rejected(self):
        image = self.fixture()
        info = smarttar.inspect_appimage(image)
        image.write_bytes(image_bytes(payload=b"changed content after approval"))
        with self.assertRaises(smarttar.SmartTarError):
            smarttar.install_appimage(info, self.apps, self.launchers)
        self.assert_unpublished()

    def test_source_replaced_since_inspection_is_rejected(self):
        image = self.fixture()
        info = smarttar.inspect_appimage(image)
        replacement = self.root / "replacement"
        replacement.write_bytes(image.read_bytes())
        replacement.replace(image)
        with self.assertRaises(smarttar.SmartTarError):
            smarttar.install_appimage(info, self.apps, self.launchers)
        self.assert_unpublished()

    def test_source_changed_during_copy_is_rejected_and_rolled_back(self):
        image = self.fixture(payload=b"x" * (smarttar.COPY_CHUNK_BYTES + 100))
        info = smarttar.inspect_appimage(image)
        real_fdopen = os.fdopen
        changed = []

        class ChangingReader(TrackedReader):
            def read(self, size=-1):
                data = super().read(size)
                if data and not changed:
                    changed.append(True)
                    with image.open("r+b") as output:
                        output.seek(-1, 2)
                        output.write(b"y")
                return data

        def changing_fdopen(descriptor, mode="r", *args, **kwargs):
            stream = real_fdopen(descriptor, mode, *args, **kwargs)
            return ChangingReader(stream, [], smarttar.COPY_CHUNK_BYTES) if mode == "rb" else stream

        with patch.object(smarttar.os, "fdopen", side_effect=changing_fdopen):
            with self.assertRaises(smarttar.SmartTarError):
                smarttar.install_appimage(info, self.apps, self.launchers)
        self.assertTrue(changed)
        self.assert_unpublished()

    def test_launcher_failure_rolls_back_and_allows_retry(self):
        image = self.fixture()
        with patch.object(smarttar, "write_desktop_file", side_effect=OSError("test launcher failure")):
            with self.assertRaises(OSError):
                self.install(image)
        self.assert_unpublished()
        self.assertTrue(self.install(image).is_dir())

    def test_insufficient_space_does_not_publish(self):
        image = self.fixture()
        with patch.object(smarttar.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaises(smarttar.SmartTarError):
                self.install(image)
        self.assert_unpublished()

    def test_special_characters_are_quoted_in_launcher(self):
        image = self.fixture('My App $cash`tick"100%.AppImage')
        installed = self.install(image)
        executable = installed / "payload" / image.name
        launcher = next(self.launchers.glob("*.desktop")).read_text()
        self.assertIn("Exec=" + smarttar.desktop_quote(str(executable)) + "\n", launcher)
        self.assertEqual(sum(line.startswith("Exec=") for line in launcher.splitlines()), 1)

    def test_control_characters_cannot_inject_a_launcher(self):
        image = self.fixture("Bad\nExec=unexpected.AppImage")
        with self.assertRaises(smarttar.SmartTarError):
            self.install(image)
        self.assertEqual(list(self.launchers.glob("*.desktop")), [])

    def test_inspect_command_routes_to_appimage_without_dialogs(self):
        image = self.fixture("Widget.Appimage")
        output = io.StringIO()
        with patch.object(sys, "argv", ["smarttar.py", "--inspect", str(image)]):
            with patch.object(smarttar, "dialog") as dialog, redirect_stdout(output):
                self.assertEqual(smarttar.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["kind"], "appimage")
        dialog.assert_not_called()

    def test_extract_command_refuses_to_execute_appimage_extraction(self):
        image = self.fixture()
        destination = self.root / "extraction"
        destination.mkdir()
        output = io.StringIO()
        with patch.object(sys, "argv", ["smarttar.py", "--extract", str(destination), str(image)]):
            with patch.object(smarttar.subprocess, "run") as run, redirect_stderr(output):
                self.assertEqual(smarttar.main(), 1)
        self.assertIn("AppImage", output.getvalue())
        self.assertEqual(list(destination.iterdir()), [])
        run.assert_not_called()

    def test_gui_routes_case_insensitive_appimage(self):
        image = self.fixture("Widget.APPIMAGE")
        with patch.object(smarttar, "run_appimage_gui") as gui:
            with patch.object(smarttar, "inspect_archive") as archive:
                smarttar.run_gui(image)
        gui.assert_called_once_with(image)
        archive.assert_not_called()

    def scripted_dialogs(self, responses):
        calls = []

        def dialog(*args):
            self.assertTrue(responses, f"Unexpected dialog {args!r}")
            option, result, output = responses.pop(0)
            self.assertEqual(args[0], option)
            calls.append(args)
            return subprocess.CompletedProcess(args, result, output, "")

        return calls, dialog

    def test_gui_install_requires_confirmation_and_does_not_launch(self):
        image = self.fixture()
        responses = [("--menu", 0, "install\n"), ("--yesno", 0, ""), ("--msgbox", 0, "")]
        calls, dialog = self.scripted_dialogs(responses)
        with patch.object(smarttar, "app_root", return_value=self.apps):
            with patch.object(smarttar, "application_dir", return_value=self.launchers):
                with patch.object(smarttar, "dialog", side_effect=dialog):
                    with patch.object(smarttar.subprocess, "Popen") as popen:
                        smarttar.run_appimage_gui(image)
        self.assertEqual(responses, [])
        self.assertIn("trust", calls[1][1].lower())
        self.assertTrue((self.launchers / "smarttar-widget-1.2.desktop").is_file())
        popen.assert_not_called()

    def test_gui_cancellation_leaves_download_and_installation_untouched(self):
        image = self.fixture()
        before = image.read_bytes(), image.stat().st_mode
        responses = [("--menu", 0, "install\n"), ("--yesno", 1, ""), ("--menu", 1, "")]
        _, dialog = self.scripted_dialogs(responses)
        with patch.object(smarttar, "app_root", return_value=self.apps):
            with patch.object(smarttar, "application_dir", return_value=self.launchers):
                with patch.object(smarttar, "dialog", side_effect=dialog):
                    smarttar.run_appimage_gui(image)
        self.assertEqual(responses, [])
        self.assertEqual((image.read_bytes(), image.stat().st_mode), before)
        self.assert_unpublished()


NATIVE_PROBE_SOURCE = r"""
/* SmartTar v0.3.0 synthetic marked-runtime launcher fixture */
#include <stdio.h>

int main(void) {
    FILE *marker = fopen(MARKER_PATH, "w");
    if (marker == NULL) {
        return 20;
    }
    fputs("deliberately launched\n", marker);
    fclose(marker);
    FILE *asset = fopen("asset.txt", "r");
    if (asset == NULL) {
        return 21;
    }
    char buffer[128];
    if (fgets(buffer, sizeof buffer, asset) == NULL) {
        fclose(asset);
        return 22;
    }
    fclose(asset);
    fputs(buffer, stdout);
    return 0;
}
"""


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc"), "A native Linux C compiler is needed")
class AppImageLaunchTests(unittest.TestCase):
    def test_locally_built_marked_runtime_launches_only_when_requested(self):
        with tempfile.TemporaryDirectory(prefix="smarttar-marked-runtime-") as temporary:
            root = Path(temporary)
            source = root / "probe.c"
            image = root / "Native Probe.AppImage"
            marker = root / "deliberately-launched.txt"
            source.write_text(
                "#define MARKER_PATH " + json.dumps(str(marker)) + "\n" + NATIVE_PROBE_SOURCE,
                encoding="utf-8",
            )
            subprocess.run(
                ["cc", "-O2", str(source), "-o", str(image)],
                capture_output=True,
                check=True,
                timeout=30,
            )
            with image.open("r+b") as stream:
                self.assertEqual(stream.read(4), b"\x7fELF")
                stream.seek(8)
                stream.write(b"AI\x02")
            image.chmod(0o644)
            info = smarttar.inspect_appimage(image)
            installed = smarttar.install_appimage(info, root / "apps", root / "launchers")
            self.assertFalse(marker.exists(), "Installation must not execute the image")
            self.assertEqual(stat.S_IMODE(image.stat().st_mode), 0o644)
            executable = installed / "payload" / image.name
            self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o755)
            launcher = root / "launchers/smarttar-native-probe.desktop"
            if shutil.which("desktop-file-validate"):
                validation = subprocess.run(
                    ["desktop-file-validate", str(launcher)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
            desktop = configparser.ConfigParser(interpolation=None)
            desktop.read(launcher, encoding="utf-8")
            entry = desktop["Desktop Entry"]
            self.assertEqual(entry["Exec"], smarttar.desktop_quote(str(executable)))
            working_directory = Path(entry["Path"])
            self.assertEqual(working_directory, executable.parent)
            expected = "SmartTar synthetic runtime launch verified\n"
            (working_directory / "asset.txt").write_text(expected, encoding="utf-8")
            result = subprocess.run(
                [str(executable)],
                cwd=working_directory,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)
            self.assertEqual(marker.read_text(), "deliberately launched\n")


if __name__ == "__main__":
    unittest.main()
