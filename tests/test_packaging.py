"""SmartTar v0.4.0 checked-source package preparation tests."""

import ast
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
VERSION = "0.4.0"
EXPECTED_FILES = {"LICENSE", "README.install", "smarttar", "smarttar-handler.desktop", "smarttar.py"}


@unittest.skipUnless(shutil.which("bash") and shutil.which("tar"), "Requires bash and tar")
class PackagePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-package-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def prepare(self, destination, project=PROJECT, extra_environment=None):
        environment = os.environ.copy()
        environment["SOURCE_DATE_EPOCH"] = "1795564800"
        if extra_environment:
            environment.update(extra_environment)
        return subprocess.run(
            ["bash", str(project / "tools/build_package.sh"), "--prepare-only", str(destination)],
            env=environment, capture_output=True, text=True, timeout=30,
        )

    def copy_inputs(self):
        source = self.root / "source"
        for relative in (
            "smarttar.py", "LICENSE", "tools/build_package.sh", "packaging/arch/PKGBUILD.in",
            "packaging/arch/smarttar", "packaging/arch/smarttar-handler.desktop", "packaging/arch/README.install",
        ):
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / relative, destination)
        return source

    def test_package_and_application_versions_match(self):
        recipe = (PROJECT / "packaging/arch/PKGBUILD.in").read_text()
        self.assertIn("pkgver=" + VERSION + "\n", recipe)
        self.assertIn("pkgrel=1\n", recipe)
        tree = ast.parse((PROJECT / "smarttar.py").read_text())
        version = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets))
        self.assertEqual(version, VERSION)

    def test_source_bundle_is_minimal_checksummed_and_deterministic(self):
        first = self.root / "first"
        second = self.root / "second"
        for output in (first, second):
            result = self.prepare(output)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        archive = first / ("smarttar-" + VERSION + ".tar")
        self.assertEqual(archive.read_bytes(), (second / archive.name).read_bytes())
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        recipe = (first / "PKGBUILD").read_text()
        self.assertIn("sha256sums=('" + checksum + "')", recipe)
        self.assertNotIn("@SOURCE_SHA256@", recipe)
        self.assertNotIn("SKIP", recipe)
        with tarfile.open(archive) as bundle:
            self.assertEqual(set(bundle.getnames()), EXPECTED_FILES)
            for member in bundle.getmembers():
                self.assertTrue(member.isfile())
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 1795564800))
                expected_mode = 0o755 if member.name in {"smarttar", "smarttar.py"} else 0o644
                self.assertEqual(member.mode, expected_mode)
            self.assertEqual(bundle.extractfile("smarttar.py").read(), (PROJECT / "smarttar.py").read_bytes())
            self.assertEqual(bundle.extractfile("LICENSE").read(), (PROJECT / "LICENSE").read_bytes())

    def test_existing_outputs_are_not_overwritten(self):
        output = self.root / "existing"
        output.mkdir()
        protected = output / "PKGBUILD"
        protected.write_bytes(b"existing file\n")
        result = self.prepare(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(protected.read_bytes(), b"existing file\n")
        self.assertEqual(set(path.name for path in output.iterdir()), {"PKGBUILD"})

    def test_version_mismatch_stops_package_preparation(self):
        source = self.copy_inputs()
        program = source / "smarttar.py"
        program.write_text(program.read_text().replace('VERSION = "0.4.0"', 'VERSION = "0.3.0"', 1))
        result = self.prepare(self.root / "output", project=source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version does not match", result.stderr)

    def test_symlink_source_is_rejected(self):
        source = self.copy_inputs()
        license_file = source / "LICENSE"
        license_file.unlink()
        license_file.symlink_to(PROJECT / "LICENSE")
        result = self.prepare(self.root / "output", project=source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)

    def test_invalid_epoch_is_rejected(self):
        result = self.prepare(self.root / "output", extra_environment={"SOURCE_DATE_EPOCH": "not-a-timestamp"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nonnegative integer", result.stderr)

    def test_handler_and_launcher_use_the_system_program(self):
        launcher = (PROJECT / "packaging/arch/smarttar").read_text()
        self.assertIn('exec /usr/bin/python3 /usr/lib/smarttar/smarttar.py "$@"', launcher)
        desktop = (PROJECT / "packaging/arch/smarttar-handler.desktop").read_text()
        self.assertIn("Exec=/usr/bin/smarttar %f\n", desktop)
        self.assertIn("NoDisplay=true\n", desktop)
        self.assertNotIn("sudo", launcher)
        self.assertNotIn("--setup", launcher)
        types = re.search(r"^MimeType=(.+)$", desktop, re.MULTILINE).group(1).split(";")
        self.assertEqual(set(filter(None, types)), {
            "application/x-compressed-tar", "application/vnd.appimage",
            "application/x-iso9660-appimage", "application/x-appimage",
        })
        if shutil.which("desktop-file-validate"):
            result = subprocess.run(["desktop-file-validate", str(PROJECT / "packaging/arch/smarttar-handler.desktop")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_recipe_declares_only_existing_runtime_dependencies(self):
        recipe = (PROJECT / "packaging/arch/PKGBUILD.in").read_text()
        self.assertIn("arch=('any')", recipe)
        self.assertIn("depends=('python>=3.10' 'kdialog' 'xdg-utils')", recipe)
        self.assertNotRegex(recipe, r"(?m)^install=")
        self.assertNotIn("systemctl", recipe)
        self.assertNotIn("/home/", recipe)
        self.assertNotIn(".config", recipe)

    def test_ci_uses_no_production_signing_secret(self):
        workflow = (PROJECT / ".github/workflows/packages.yml").read_text()
        self.assertNotIn("secrets.", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("- packages", workflow)
        self.assertIn("runuser -u smarttar-build", workflow)
        helper = (PROJECT / "tools/test_arch_package.sh").read_text()
        self.assertIn("SMARTTAR_DISPOSABLE_CONTAINER", helper)
        self.assertIn("SigLevel = Required TrustedOnly", helper)
        self.assertNotIn("TrustAll", helper)
        self.assertNotIn("SigLevel = Never", helper)
        self.assertIn('pacman "${pacman_arguments[@]}" -Syu', helper)

    def test_test_helper_refuses_unscoped_execution(self):
        environment = os.environ.copy()
        environment.pop("SMARTTAR_DISPOSABLE_CONTAINER", None)
        result = subprocess.run(["bash", str(PROJECT / "tools/test_arch_package.sh"), "/nonexistent/package"], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("disposable Arch test container", result.stderr)


if __name__ == "__main__":
    unittest.main()
