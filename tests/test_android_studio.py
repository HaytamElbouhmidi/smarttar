"""SmartTar v0.2.0 Android Studio recipe tests using local, non-vendor fixtures."""

import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import smarttar


_DEFAULT = object()


@unittest.skipUnless(shutil.which("true"), "A local Linux ELF fixture is required")
class AndroidStudioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-android-studio-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install_root = self.root / "installed"
        self.launchers = self.root / "launchers"
        self.native = Path(shutil.which("true")).read_bytes()
        if not self.native.startswith(b"\x7fELF"):
            self.skipTest("The local true executable is not an ELF file")
        self.shell = b"#!/bin/sh\n# SmartTar v0.2.0 test fixture\nexit 0\n"
        self.counter = 0
        machine = patch.object(smarttar.platform, "machine", return_value="x86_64")
        machine.start()
        self.addCleanup(machine.stop)

    def metadata(self, launcher="bin/studio", arch="amd64"):
        launch = {"os": "Linux", "launcherPath": launcher}
        if arch is not None:
            launch["arch"] = arch
        return {
            "name": "Android Studio",
            "version": "fixture",
            "buildNumber": "262.1",
            "productCode": "AI",
            "launch": [launch],
        }

    def archive(self, *, metadata=_DEFAULT, launchers=None, build=None, extra=(), omit=()):
        self.counter += 1
        archive = self.root / f"android-studio-fixture-{self.counter}.tar.gz"
        if metadata is _DEFAULT:
            metadata = self.metadata()
        if launchers is None:
            launchers = [("bin/studio", self.native, 0o755)]
        members = [
            ("jbr/bin/java", self.native, 0o755),
            ("lib/something.jar", b"Local library fixture", 0o644),
            ("bin/fsnotifier", self.native, 0o755),
            ("bin/restarter", self.native, 0o755),
            *launchers,
        ]
        if metadata is not None:
            payload = metadata if isinstance(metadata, bytes) else json.dumps(metadata).encode("utf-8")
            members.append(("product-info.json", payload, 0o644))
        if build is not None:
            members.append(("build.txt", build, 0o644))
        members.extend(extra)
        with tarfile.open(archive, "w:gz") as tar:
            for relative, payload, mode in members:
                if relative in omit:
                    continue
                member = tarfile.TarInfo("android-studio/" + relative)
                member.size = len(payload)
                member.mode = mode
                tar.addfile(member, io.BytesIO(payload))
        return archive

    def inspect(self, archive):
        with patch.object(smarttar.subprocess, "run", side_effect=AssertionError("Inspection must not run a program")):
            return smarttar.inspect_archive(archive)

    def assert_unsupported_bundle(self, archive):
        info = self.inspect(archive)
        self.assertEqual(info.kind, "android-studio")
        self.assertEqual(info.binaries, (), "An unsupported IDE must not fall back to its bundled Java runtime")
        return info

    def test_current_native_launcher_excludes_runtime_and_helpers(self):
        info = self.inspect(self.archive())
        self.assertEqual(info.kind, "android-studio")
        self.assertEqual(info.binaries, ("android-studio/bin/studio",))
        self.assertNotIn("android-studio/jbr/bin/java", info.binaries)
        self.assertNotIn("android-studio/bin/fsnotifier", info.binaries)
        self.assertNotIn("android-studio/bin/restarter", info.binaries)

    def test_native_install_keeps_bundle_and_names_the_launcher(self):
        info = self.inspect(self.archive())
        with patch.object(smarttar.subprocess, "run", side_effect=AssertionError("Installation must not execute a program")), \
                patch.object(smarttar.subprocess, "Popen", side_effect=AssertionError("Installation must not start a process")):
            installed = smarttar.install_portable(info, info.binaries[0], self.install_root, self.launchers)
        bundle = installed / "payload/android-studio"
        self.assertEqual((bundle / "bin/studio").read_bytes(), self.native)
        self.assertEqual((bundle / "jbr/bin/java").read_bytes(), self.native)
        self.assertEqual((bundle / "lib/something.jar").read_bytes(), b"Local library fixture")
        self.assertTrue((bundle / "product-info.json").is_file())
        desktop = next(self.launchers.glob("*.desktop")).read_text(encoding="utf-8")
        self.assertIn("\nName=Android Studio\n", desktop)
        self.assertIn(f"\nExec={smarttar.desktop_quote(str(bundle / 'bin/studio'))}\n", desktop)
        self.assertIn(f"\nPath={smarttar.desktop_string(str(bundle / 'bin'))}\n", desktop)

    def test_earlier_shell_launcher_installs_without_running(self):
        archive = self.archive(
            metadata=self.metadata("bin/studio.sh"),
            launchers=[("bin/studio.sh", self.shell, 0o755)],
        )
        info = self.inspect(archive)
        self.assertEqual(info.kind, "android-studio")
        self.assertEqual(info.binaries, ("android-studio/bin/studio.sh",))
        with patch.object(smarttar.subprocess, "run", side_effect=AssertionError("The shell launcher must not execute")), \
                patch.object(smarttar.subprocess, "Popen", side_effect=AssertionError("The shell launcher must not start")):
            installed = smarttar.install_portable(info, info.binaries[0], self.install_root, self.launchers)
        script = installed / "payload/android-studio/bin/studio.sh"
        self.assertEqual(script.read_bytes(), self.shell)
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)
        desktop = next(self.launchers.glob("*.desktop")).read_text(encoding="utf-8")
        self.assertIn(f"\nExec={smarttar.desktop_quote(str(script))}\n", desktop)
        self.assertNotIn("sh -c", desktop)

    def test_ai_build_fallback_uses_known_native_launcher(self):
        info = self.inspect(self.archive(
            metadata=None,
            build=b"AI-262.1\n",
            launchers=[("bin/studio", self.native, 0o755), ("bin/studio.sh", self.shell, 0o755)],
        ))
        self.assertEqual(info.kind, "android-studio")
        self.assertEqual(info.binaries, ("android-studio/bin/studio",))

    def test_ai_build_fallback_supports_earlier_shell_launcher(self):
        info = self.inspect(self.archive(
            metadata=None,
            build=b"AI-262.1\n",
            launchers=[("bin/studio.sh", self.shell, 0o755)],
        ))
        self.assertEqual(info.kind, "android-studio")
        self.assertEqual(info.binaries, ("android-studio/bin/studio.sh",))

    def test_older_metadata_without_architecture_remains_supported(self):
        info = self.inspect(self.archive(metadata=self.metadata(arch=None)))
        self.assertEqual(info.binaries, ("android-studio/bin/studio",))

    def test_mismatched_architecture_never_offers_java(self):
        self.assert_unsupported_bundle(self.archive(metadata=self.metadata(arch="aarch64")))

    def test_non_linux_launch_metadata_never_offers_java(self):
        metadata = self.metadata()
        metadata["launch"][0]["os"] = "Windows"
        self.assert_unsupported_bundle(self.archive(metadata=metadata))

    def test_unsupported_launcher_paths_never_offer_java(self):
        for launcher in (
            "../outside",
            "/tmp/outside",
            "bin/../jbr/bin/java",
            "jbr/bin/java",
            "bin/studio --unsafe-argument",
            "bin/studio\n",
            None,
            ["bin/studio"],
        ):
            with self.subTest(launcher=launcher):
                self.assert_unsupported_bundle(self.archive(metadata=self.metadata(launcher)))

    def test_invalid_launch_metadata_never_falls_back_to_build_file(self):
        for launch in (None, {}, "bin/studio", [None], [{"os": "Linux"}]):
            with self.subTest(launch=launch):
                metadata = self.metadata()
                metadata["launch"] = launch
                self.assert_unsupported_bundle(self.archive(metadata=metadata, build=b"AI-262.1\n"))

    def test_malformed_metadata_in_recognizable_bundle_never_offers_java(self):
        self.assert_unsupported_bundle(self.archive(metadata=b'{"productCode":"AI","launch":'))

    def test_missing_or_non_executable_native_launcher_never_offers_java(self):
        for launchers in ([], [("bin/studio", self.native, 0o644)]):
            with self.subTest(launchers_present=bool(launchers)):
                self.assert_unsupported_bundle(self.archive(launchers=launchers))

    def test_invalid_native_header_never_offers_java(self):
        self.assert_unsupported_bundle(self.archive(launchers=[("bin/studio", b"Not an ELF program", 0o755)]))

    def test_unknown_shell_interpreter_never_offers_java(self):
        self.assert_unsupported_bundle(self.archive(
            metadata=self.metadata("bin/studio.sh"),
            launchers=[("bin/studio.sh", b"#!/usr/bin/python3\npass\n", 0o755)],
        ))

    def test_crlf_shell_shebang_is_not_a_launchable_linux_script(self):
        self.assert_unsupported_bundle(self.archive(
            metadata=self.metadata("bin/studio.sh"),
            launchers=[("bin/studio.sh", b"#!/bin/sh\r\nexit 0\r\n", 0o755)],
        ))

    def test_incomplete_runtime_or_library_layout_is_not_installable(self):
        for omitted in ("jbr/bin/java", "lib/something.jar"):
            with self.subTest(omitted=omitted):
                self.assert_unsupported_bundle(self.archive(omit=(omitted,)))

    def test_gui_offers_android_studio_by_name(self):
        archive = self.archive()
        calls = []

        def respond(*args):
            self.assertEqual(args[0], "kdialog")
            self.assertIn("--menu", args)
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "")

        with patch.object(smarttar, "run_command", side_effect=respond), \
                patch.object(smarttar.shutil, "which", return_value="/test/kdialog"):
            smarttar.run_gui(archive)
        self.assertEqual(len(calls), 1)
        self.assertIn("Install Android Studio", calls[0])
        self.assertNotIn("Install portable application", calls[0])

    def test_gui_does_not_offer_install_for_unsupported_bundle(self):
        archive = self.archive(metadata=self.metadata(arch="aarch64"))
        calls = []

        def respond(*args):
            self.assertEqual(args[0], "kdialog")
            self.assertIn("--menu", args)
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "")

        with patch.object(smarttar, "run_command", side_effect=respond), \
                patch.object(smarttar.shutil, "which", return_value="/test/kdialog"):
            smarttar.run_gui(archive)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("install", calls[0])
        self.assertIn("extract", calls[0])

    def test_insufficient_disk_space_leaves_destination_absent(self):
        info = self.inspect(self.archive())
        destination = self.root / "not-created" / "extracted"
        with patch.object(smarttar.shutil, "disk_usage", return_value=SimpleNamespace(free=0)) as usage:
            with self.assertRaisesRegex(smarttar.SmartTarError, "Not enough free disk space"):
                smarttar.unpack_checked(info, destination)
        usage.assert_called_once_with(self.root)
        self.assertFalse(destination.exists())
        self.assertFalse(destination.parent.exists())


if __name__ == "__main__":
    unittest.main()
