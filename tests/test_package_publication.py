"""SmartTar v0.4.0 trusted-release publication checks without production secrets."""

from contextlib import contextmanager
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("smarttar_package_publication", PROJECT / "tools/publish_package_repository.py")
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)
VERSION = "0.4.0"
COMMIT = "1" * 40
MAIN = "2" * 40
PACKAGE = "smarttar-0.4.0-1-any.pkg.tar.zst"


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-publication-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def release(self):
        return {
            "schema_version": 1, "repository": publication.REPOSITORY,
            "tag": "v" + VERSION, "version": VERSION, "package_release": 1,
            "source_commit": COMMIT, "trusted_main_commit": MAIN,
            "signing_fingerprint": publication.SIGNING_FINGERPRINT,
        }

    def evidence(self):
        package = self.root / PACKAGE
        package.write_bytes(b"Disposable package fixture, not an installable package.\n")
        checksum = hashlib.sha256(package.read_bytes()).hexdigest()
        build = {
            "version": VERSION, "package_release": 1, "architecture": "any",
            "package": PACKAGE, "package_sha256": checksum, "source_sha256": "a" * 64,
        }
        report = {
            "status": "passed", "version": VERSION, "package_release": 1,
            "package_sha256": checksum, "production_signing_key_used": False,
            "package_layout_verified": True, "package_signature_verified": True,
            "repository_database_signature_verified": True, "tampered_package_rejected": True,
            "unsigned_package_rejected": True, "tampered_database_rejected": True,
            "existing_apps_shortcuts_and_preferences_preserved": True,
            "upgrade": {"to": VERSION + "-1", "isolated_root": True, "command": "pacman -Syu"},
        }
        report_path = self.root / "package-test.json"
        report_path.write_text(json.dumps(report))
        (self.root / "package-build.json").write_text(json.dumps(build))
        return package, report_path, build, report

    def database(self, destination, fields=None, extra=None):
        values = {
            "NAME": "smarttar", "VERSION": "0.4.0-1", "ARCH": "any",
            "FILENAME": PACKAGE, "SHA256SUM": "b" * 64,
        }
        if fields:
            values.update(fields)
        text = "".join(f"%{key}%\n{value}\n\n" for key, value in values.items())
        payload = text.encode()
        with tarfile.open(destination, "w:gz") as archive:
            member = tarfile.TarInfo("smarttar-0.4.0-1/desc")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
            if extra:
                added = tarfile.TarInfo(extra)
                added.size = len(payload)
                archive.addfile(added, io.BytesIO(payload))
        return destination

    def test_release_tags_accept_only_exact_stable_versions(self):
        for accepted in ("v0.4.0", "v1.0.0", "v12.34.56"):
            self.assertEqual(publication.release_tag(accepted), accepted[1:])
        for rejected in ("main", "v01.4.0", "v0.4.0-dev", "v0.4.0+build", "refs/tags/v0.4.0", "v0.4.0\n", "--help", None):
            with self.subTest(tag=rejected), self.assertRaises(publication.PublicationError):
                publication.release_tag(rejected)

    def test_version_is_inspected_without_executing_release_code(self):
        self.assertEqual(publication.read_version('raise RuntimeError("Do not execute")\nVERSION = "0.4.0"\n'), VERSION)
        for source in ('VERSION = str("0.4.0")', 'VERSION = "0.4.0"\nVERSION = "0.5.0"', "VERSION = 4"):
            with self.subTest(source=source), self.assertRaises(publication.PublicationError):
                publication.read_version(source)

    def test_release_validation_checks_main_ancestry_before_source(self):
        calls = []

        def git_result(arguments, **unused):
            calls.append(arguments)
            operation = arguments[3:]
            if operation == ["remote", "get-url", "origin"]:
                return b"https://github.com/HaytamElbouhmidi/smarttar.git\n"
            if operation in (["rev-parse", "HEAD"], ["rev-parse", "refs/remotes/origin/main"]):
                return (MAIN + "\n").encode()
            if operation == ["rev-parse", "--verify", "refs/tags/v0.4.0^{commit}"]:
                return (COMMIT + "\n").encode()
            if operation == ["merge-base", "--is-ancestor", COMMIT, MAIN]:
                return b""
            if operation == ["show", COMMIT + ":smarttar.py"]:
                return b'VERSION = "0.4.0"\n'
            if operation == ["show", COMMIT + ":packaging/arch/PKGBUILD.in"]:
                return b"pkgver=0.4.0\npkgrel=1\n"
            self.fail(f"Unexpected release-validation operation: {operation}")

        with mock.patch.object(publication, "command", side_effect=git_result), mock.patch.dict(os.environ, {}, clear=True):
            publication.validate_release(self.root, "v0.4.0", self.root / "verified.json")
        self.assertEqual(json.loads((self.root / "verified.json").read_text()), self.release())
        ancestry = next(index for index, call in enumerate(calls) if "--is-ancestor" in call)
        source = next(index for index, call in enumerate(calls) if "show" in call)
        self.assertLess(ancestry, source)

    def test_release_records_reject_changed_identity_or_key(self):
        path = self.root / "verified.json"
        mutations = {
            "schema_version": 2, "repository": "someone/smarttar", "version": "0.3.0",
            "signing_fingerprint": "0" * 40, "source_commit": None,
            "trusted_main_commit": "main", "package_release": True,
        }
        for field, changed in mutations.items():
            record = self.release()
            record[field] = changed
            path.write_text(json.dumps(record))
            with self.subTest(field=field), self.assertRaises(publication.PublicationError):
                publication.verify_release_record(path)

    def test_matching_package_evidence_is_accepted(self):
        package, report, build, unused = self.evidence()
        self.assertEqual(publication.validate_package_evidence(package, report, self.release()), (build["package_sha256"], build["source_sha256"]))

    def test_package_changes_after_testing_are_rejected(self):
        package, report, unused_build, unused_report = self.evidence()
        package.write_bytes(package.read_bytes() + b"changed\n")
        with self.assertRaises(publication.PublicationError):
            publication.validate_package_evidence(package, report, self.release())

    def test_every_required_test_check_must_be_true(self):
        package, path, unused, report = self.evidence()
        for field in (
            "package_layout_verified", "package_signature_verified", "repository_database_signature_verified",
            "tampered_package_rejected", "unsigned_package_rejected", "tampered_database_rejected",
            "existing_apps_shortcuts_and_preferences_preserved",
        ):
            changed = dict(report, **{field: False})
            path.write_text(json.dumps(changed))
            with self.subTest(field=field), self.assertRaises(publication.PublicationError):
                publication.validate_package_evidence(package, path, self.release())

    def test_upgrade_and_disposable_key_evidence_are_required(self):
        package, path, unused, report = self.evidence()
        for mutation in ({"upgrade": []}, {"upgrade": {"to": "0.4.0-1"}}, {"production_signing_key_used": True}):
            path.write_text(json.dumps(dict(report, **mutation)))
            with self.subTest(mutation=mutation), self.assertRaises(publication.PublicationError):
                publication.validate_package_evidence(package, path, self.release())

    def test_build_record_must_match_the_exact_package(self):
        package, report, build, unused = self.evidence()
        path = self.root / "package-build.json"
        for mutation in ({"package": "other.pkg.tar.zst"}, {"architecture": "x86_64"}, {"package_release": 2}, {"source_sha256": None}):
            path.write_text(json.dumps(dict(build, **mutation)))
            with self.subTest(mutation=mutation), self.assertRaises(publication.PublicationError):
                publication.validate_package_evidence(package, report, self.release())

    def test_database_requires_one_expected_package(self):
        path = self.database(self.root / "smarttar.db")
        self.assertEqual(publication.database_entry(path)["FILENAME"], PACKAGE)
        for fields in ({"NAME": "other"}, {"ARCH": "x86_64"}, {"FILENAME": "../escape.pkg.tar.zst"}, {"VERSION": "0.3.0-1"}, {"SHA256SUM": "bad"}):
            self.database(path, fields)
            with self.subTest(fields=fields), self.assertRaises(publication.PublicationError):
                publication.database_entry(path)
        self.database(path, extra="another-1/desc")
        with self.assertRaises(publication.PublicationError):
            publication.database_entry(path)

    def test_database_expansion_is_bounded_before_tar_parsing(self):
        path = self.root / "bomb.db"
        path.write_bytes(gzip.compress(b"x" * (publication.MAX_DATABASE_EXPANDED_BYTES + 1)))
        with mock.patch.object(publication.tarfile, "open") as parser, self.assertRaises(publication.PublicationError):
            publication.database_entry(path)
        parser.assert_not_called()

    def test_distribution_accepts_only_regular_artifacts(self):
        repository = self.root / "distribution"
        repository.mkdir()
        (repository / ".git").mkdir()
        (repository / "any").mkdir()
        (repository / "README.md").write_text("# Signed packages\n")
        archive = repository / "any/smarttar.db.tar.gz"
        archive.write_bytes(b"fixture")
        publication.validate_distribution_tree(repository)
        alias = repository / "any/smarttar.db"
        alias.symlink_to(archive.name)
        with self.assertRaises(publication.PublicationError):
            publication.validate_distribution_tree(repository)
        alias.unlink()
        (repository / ".github").mkdir()
        with self.assertRaises(publication.PublicationError):
            publication.validate_distribution_tree(repository)

    def test_artifact_allowlist_rejects_source_and_unexpected_names(self):
        for name in (PACKAGE, PACKAGE + ".sig", PACKAGE + ".release.json", PACKAGE + ".release.json.sig", "smarttar.db", "smarttar.files.tar.gz.sig", "smarttar.asc", "release.json.sig"):
            self.assertTrue(publication.valid_artifact_name(name), name)
        for name in ("smarttar.py", "PKGBUILD", "private.asc", "smarttar.db.old", "smarttar.db.tar.xz", "smarttar-01.4.0-1-any.pkg.tar.zst", "../" + PACKAGE):
            self.assertFalse(publication.valid_artifact_name(name), name)

    def test_public_and_private_fingerprint_listing_ignores_subkeys(self):
        fingerprint = publication.SIGNING_FINGERPRINT
        listing = "pub:::::::::\nfpr:::::::::" + fingerprint + ":\nsub:::::::::\nfpr:::::::::" + "3" * 40 + ":\n"
        self.assertEqual(publication.primary_fingerprints(listing, "pub"), [fingerprint])
        self.assertEqual(publication.primary_fingerprints(listing.replace("pub", "sec").replace("sub", "ssb"), "sec"), [fingerprint])

    def test_signature_requires_complete_pinned_primary_fingerprint(self):
        package = self.root / "artifact"
        package.write_bytes(b"test artifact")
        Path(str(package) + ".sig").write_bytes(b"test signature")
        keyring = self.root / "public.gpg"
        valid = f"[GNUPG:] VALIDSIG {'4' * 40} 2026-11-25 1795564800 0 4 0 1 10 00 {publication.SIGNING_FINGERPRINT}\n"
        with mock.patch.object(publication, "command", return_value=valid.encode()):
            publication.verify_signature(package, keyring)
        failures = [
            valid.replace(publication.SIGNING_FINGERPRINT, "5" * 40),
            "[GNUPG:] VALIDSIG " + publication.SIGNING_FINGERPRINT + "\n",
            valid + valid,
        ]
        for error in ("BADSIG", "ERRSIG", "EXPKEYSIG", "REVKEYSIG", "KEYEXPIRED", "SIGEXPIRED", "FAILURE", "NO_PUBKEY"):
            failures.append(valid + "[GNUPG:] " + error + " unexpected\n")
        for status in failures:
            with self.subTest(status=status), mock.patch.object(publication, "command", return_value=status.encode()), self.assertRaises(publication.PublicationError):
                publication.verify_signature(package, keyring)

    def test_public_key_reads_do_not_start_a_private_key_agent(self):
        key = self.root / "public.asc"
        key.write_text("Public key fixture")
        listing = "pub:::::::::\nfpr:::::::::" + publication.SIGNING_FINGERPRINT + ":\n"
        with mock.patch.object(publication, "command", side_effect=[listing.encode(), b""]) as calls:
            with publication.verification_key(key):
                pass
        self.assertEqual(len(calls.call_args_list), 2)
        for call in calls.call_args_list:
            self.assertIn("--no-autostart", call.args[0])
        key.write_text("-----BEGIN PGP PRIVATE KEY BLOCK-----")
        with mock.patch.object(publication, "command") as calls, self.assertRaises(publication.PublicationError):
            with publication.verification_key(key):
                pass
        calls.assert_not_called()

    def test_command_errors_do_not_expose_tool_diagnostics(self):
        completed = subprocess.CompletedProcess(["test"], 1, b"sensitive output", b"sensitive error")
        with mock.patch.object(publication.subprocess, "run", return_value=completed), self.assertRaises(publication.PublicationError) as raised:
            publication.command(["test"], label="Test operation", data=b"fixture, not a production key")
        self.assertNotIn("sensitive", str(raised.exception))

    def test_missing_or_invalid_release_stops_before_key_import(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(publication, "command") as calls, self.assertRaises(publication.PublicationError):
            publication.publish(self.root / PACKAGE, self.root / "report.json", self.root / "public.asc", self.root / "missing.json", self.root, self.root / "result.json")
        calls.assert_not_called()
        with mock.patch.dict(os.environ, {"SMARTTAR_SIGNING_KEY": "not a real key"}), mock.patch.object(publication, "command") as calls, self.assertRaises(publication.PublicationError):
            try:
                publication.publish(self.root / PACKAGE, self.root / "report.json", self.root / "public.asc", self.root / "missing.json", self.root, self.root / "result.json")
            finally:
                self.assertNotIn("SMARTTAR_SIGNING_KEY", os.environ)
        calls.assert_not_called()

    def test_identical_release_reuses_immutable_published_package(self):
        package, report, build, unused = self.evidence()
        verified = self.root / "verified.json"
        verified.write_text(json.dumps(self.release()))
        repository = self.root / "distribution"
        artifacts = repository / "any"
        artifacts.mkdir(parents=True)
        prior_package = artifacts / PACKAGE
        prior_package.write_bytes(b"Earlier build from exactly the same source.\n")
        prior_sha256 = hashlib.sha256(prior_package.read_bytes()).hexdigest()
        database = self.database(artifacts / "smarttar.db", {"SHA256SUM": prior_sha256})
        (artifacts / "smarttar.db.tar.gz").write_bytes(database.read_bytes())
        Path(str(prior_package) + ".release.json").write_text(json.dumps({"source_commit": COMMIT, "source_sha256": build["source_sha256"], "package_sha256": prior_sha256}))

        @contextmanager
        def mock_key(unused):
            yield self.root / "keyring.gpg"

        result = self.root / "result.json"
        with mock.patch.dict(os.environ, {"SMARTTAR_SIGNING_KEY": "not a real key"}), mock.patch.object(publication, "verification_key", mock_key), mock.patch.object(publication, "verify_signature"), mock.patch.object(publication, "command") as commands:
            publication.publish(package, report, self.root / "public.asc", verified, repository, result)
        commands.assert_not_called()
        self.assertEqual(json.loads(result.read_text())["status"], "already_current")
        self.assertEqual(hashlib.sha256(prior_package.read_bytes()).hexdigest(), prior_sha256)

    def test_production_workflow_keeps_secret_in_one_post_test_step(self):
        workflow = (PROJECT / ".github/workflows/publish-packages.yml").read_text()
        self.assertIn("release_tag:", workflow)
        self.assertIn("github.repository == 'HaytamElbouhmidi/smarttar'", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertNotIn("pull_request", workflow)
        self.assertEqual(workflow.count("secrets.SMARTTAR_SIGNING_KEY"), 1)
        self.assertIn("Run all application regressions without release credentials", workflow)
        self.assertIn("python3 -m unittest discover -s tests -v", workflow)
        self.assertLess(workflow.index("Run all application regressions"), workflow.index("secrets.SMARTTAR_SIGNING_KEY"))
        self.assertLess(workflow.index("Test a signed upgrade"), workflow.index("secrets.SMARTTAR_SIGNING_KEY"))
        self.assertLess(workflow.index("validate-release"), workflow.index("runuser -u smarttar-build"))
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotRegex(workflow, r"push[^\n]*--force")
        self.assertIn("HEAD:refs/heads/packages", workflow)
        self.assertIn("bash release/tools/test_live_repository.sh", workflow)
        self.assertIn("pacman-key --init", workflow)
        self.assertIn("SMARTTAR_LIVE_RESULT:", workflow)
        self.assertIn("/tmp/smarttar-release-output/*.json", workflow)
        self.assertNotIn("signing-*/", workflow)

    def test_unexpected_download_destinations_are_rejected_without_network(self):
        for url in ("http://example.com/package", publication.SERVER + ".attacker.example/package", "https://github.com/example/package"):
            with self.subTest(url=url), mock.patch.object(publication.urllib.request, "urlopen") as request, self.assertRaises(publication.PublicationError):
                publication.download(url, self.root / "package", 1024)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
