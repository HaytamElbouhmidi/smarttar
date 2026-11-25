"""SmartTar v0.4.0 signed repository bootstrap checks without root changes."""

from contextlib import redirect_stderr, redirect_stdout
import gzip
import importlib.util
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch
import urllib.error
import urllib.request


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("smarttar_repository_setup", PROJECT / "tools/configure_repository.py")
repository = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repository)
NOW = 1800000000
OTHER_FINGERPRINT = "A" * 40


def key_listing(fingerprint=repository.FINGERPRINT, *, validity="-", created=1700000000, expires="", capabilities="scSC"):
    row = ["pub", validity, "255", "22", fingerprint[-16:], str(created), str(expires), "", "", "", "", capabilities]
    fpr = ["fpr", "", "", "", "", "", "", "", "", fingerprint, ""]
    return ":".join(row) + "\n" + ":".join(fpr) + "\n"


def signature_status(signer=repository.FINGERPRINT, primary=repository.FINGERPRINT):
    return f"[GNUPG:] VALIDSIG {signer} 2026-11-25 1795636800 0 4 0 22 8 00 {primary}\n"


def descriptor(**changes):
    fields = {
        "NAME": "smarttar", "VERSION": "0.4.0-1", "ARCH": "any",
        "FILENAME": "smarttar-0.4.0-1-any.pkg.tar.zst", "SHA256SUM": "a" * 64, "PGPSIG": "AQID",
    }
    fields.update(changes)
    return "\n\n".join(f"%{key}%\n{value}" for key, value in fields.items()) + "\n"


def database(entries=None):
    entries = entries if entries is not None else [("smarttar-0.4.0-1/desc", descriptor().encode())]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, payload in entries:
            member = tarfile.TarInfo(name)
            if isinstance(payload, bytes):
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            else:
                member.type = payload
                if payload in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    member.linkname = "other"
                archive.addfile(member)
    return output.getvalue()


class KeyAndSignatureTests(unittest.TestCase):
    def test_pinned_public_key_is_accepted(self):
        repository.validate_key_listing(key_listing(), now=NOW)

    def test_different_primary_fingerprint_is_rejected(self):
        with self.assertRaisesRegex(repository.SetupError, "pinned"):
            repository.validate_key_listing(key_listing(OTHER_FINGERPRINT), now=NOW)

    def test_multiple_public_keys_and_secret_records_are_rejected(self):
        for listing in (
            key_listing() + key_listing(OTHER_FINGERPRINT),
            key_listing() + "sec:-:255:22:::::::scSC:\n",
            key_listing() + "ssb:-:255:22:::::::s:\n",
        ):
            with self.subTest(listing=listing):
                with self.assertRaises(repository.SetupError):
                    repository.validate_key_listing(listing, now=NOW)

    def test_revoked_expired_disabled_invalid_and_nonsigning_keys_are_rejected(self):
        for validity in ("r", "e", "d", "i"):
            with self.subTest(validity=validity):
                with self.assertRaises(repository.SetupError):
                    repository.validate_key_listing(key_listing(validity=validity), now=NOW)
        with self.assertRaises(repository.SetupError):
            repository.validate_key_listing(key_listing(capabilities="ecEC"), now=NOW)

    def test_invalid_dates_future_creation_and_expiration_are_rejected(self):
        for listing in (
            key_listing(created="not-a-date"), key_listing(expires="invalid"),
            key_listing(created=NOW + 301), key_listing(expires=NOW), key_listing(expires=NOW - 1),
        ):
            with self.subTest(listing=listing):
                with self.assertRaises(repository.SetupError):
                    repository.validate_key_listing(listing, now=NOW)

    def test_missing_or_malformed_fingerprint_records_are_rejected(self):
        for listing in ("pub:-:255:22:::::::scSC:\n", key_listing().splitlines()[0] + "\nfpr::::\n"):
            with self.subTest(listing=listing):
                with self.assertRaises(repository.SetupError):
                    repository.validate_key_listing(listing, now=NOW)

    def test_armored_key_screen_rejects_private_binary_and_combined_inputs(self):
        public = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nfixture\n-----END PGP PUBLIC KEY BLOCK-----\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "supplied.asc"
            workspace = root / "workspace"
            workspace.mkdir()
            for payload in (
                public.replace(b"PUBLIC", b"PRIVATE"), b"binary data", public + public,
                public + b"unrelated data", public.replace(b"fixture", b"PRIVATE KEY"),
            ):
                with self.subTest(payload=payload):
                    source.write_bytes(payload)
                    with patch.object(repository, "run_tool") as run:
                        with self.assertRaises(repository.SetupError):
                            repository.prepare_public_key(source, workspace)
                    run.assert_not_called()

    def test_preparation_uses_only_public_inspection_and_dearmor(self):
        public = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nfixture\n-----END PGP PUBLIC KEY BLOCK-----\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.asc"
            source.write_bytes(public)
            workspace = root / "workspace"
            workspace.mkdir()
            with patch.object(repository, "run_tool", side_effect=[key_listing(), ""]) as run:
                snapshot, keyring = repository.prepare_public_key(source, workspace)
            self.assertEqual(snapshot.read_bytes(), public)
            self.assertEqual(keyring, workspace / "public.gpg")
            calls = [item.args for item in run.call_args_list]
            self.assertEqual(len(calls), 2)
            self.assertIn("--show-keys", calls[0])
            self.assertIn("--dearmor", calls[1])
            for args in calls:
                self.assertIn("--no-autostart", args)
                self.assertIn("--no-options", args)
                self.assertNotIn("--import", args)
                self.assertNotIn("--import-secret-keys", args)

    def test_valid_primary_and_subkey_signature_statuses_are_accepted(self):
        repository.verify_signature_status(signature_status())
        repository.verify_signature_status(signature_status(signer=OTHER_FINGERPRINT))

    def test_unpinned_primary_bad_and_ambiguous_signatures_are_rejected(self):
        values = [signature_status(primary=OTHER_FINGERPRINT), signature_status() * 2, "", f"[GNUPG:] VALIDSIG {repository.FINGERPRINT}\n"]
        values += [signature_status() + f"[GNUPG:] {failure} rejected\n" for failure in (
            "BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG", "NO_PUBKEY", "NODATA", "FAILURE",
            "KEYEXPIRED", "SIGEXPIRED", "KEYREVOKED", "ERROR",
        )]
        for status in values:
            with self.subTest(status=status):
                with self.assertRaises(repository.SetupError):
                    repository.verify_signature_status(status)

    def test_malformed_signature_fields_are_rejected(self):
        for status in (
            signature_status(signer="bad"), signature_status().replace("2026-11-25", "not-a-date"),
            signature_status().replace("1795636800", "timestamp"), signature_status().replace(" 22 ", " algorithm "),
        ):
            with self.subTest(status=status):
                with self.assertRaises(repository.SetupError):
                    repository.verify_signature_status(status)


class MetadataTests(unittest.TestCase):
    def test_expected_single_package_record_is_accepted(self):
        self.assertEqual(repository.inspect_database(database()), "0.4.0-1")

    def test_wrong_package_identity_architecture_filename_checksum_or_signature_is_rejected(self):
        changes = (
            {"NAME": "other"}, {"ARCH": "x86_64"}, {"FILENAME": "../smarttar.pkg.tar.zst"},
            {"SHA256SUM": "not-a-checksum"}, {"PGPSIG": ""}, {"VERSION": "0.4.0-0"}, {"VERSION": "0.4.0"},
        )
        for fields in changes:
            with self.subTest(fields=fields):
                with self.assertRaises(repository.SetupError):
                    repository.inspect_database(database([("package/desc", descriptor(**fields).encode())]))

    def test_duplicate_fields_empty_database_and_multiple_packages_are_rejected(self):
        values = [
            database([]), database([("a/desc", descriptor().encode()), ("b/desc", descriptor().encode())]),
            database([("a/desc", (descriptor() + "\n%NAME%\nsmarttar\n").encode())]),
        ]
        for value in values:
            with self.subTest(size=len(value)):
                with self.assertRaises(repository.SetupError):
                    repository.inspect_database(value)

    def test_unsafe_paths_links_special_files_and_unexpected_members_are_rejected(self):
        entries = (
            [("/absolute/desc", descriptor().encode())], [("../escape/desc", descriptor().encode())],
            [("package/unexpected", b"data")], [("package/desc", tarfile.SYMTYPE)],
            [("package/desc", tarfile.LNKTYPE)], [("package/desc", tarfile.FIFOTYPE)],
        )
        for items in entries:
            with self.subTest(items=items):
                with self.assertRaises(repository.SetupError):
                    repository.inspect_database(database(items))

    def test_malformed_gzip_and_non_utf8_metadata_are_rejected(self):
        for content in (b"not gzip", database()[:20], database([("package/desc", b"bad\xfftext")])):
            with self.subTest(size=len(content)):
                with self.assertRaises(repository.SetupError):
                    repository.inspect_database(content)

    def test_expanded_stream_cap_includes_hidden_tar_metadata(self):
        contents = gzip.compress(b"\0" * (repository.MAX_DATABASE + 1), mtime=0)
        self.assertLess(len(contents), 10000)
        with self.assertRaisesRegex(repository.SetupError, "expanded.*too large"):
            repository.inspect_database(contents)

    def test_member_count_and_descriptor_size_are_bounded(self):
        values = (
            database([(f"directory-{number}", tarfile.DIRTYPE) for number in range(65)]),
            database([("package/desc", b"a" * (128 * 1024 + 1))]),
        )
        for value in values:
            with self.subTest(size=len(value)):
                with self.assertRaises(repository.SetupError):
                    repository.inspect_database(value)

    def test_signature_validation_precedes_database_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with patch.object(repository, "download", side_effect=[b"database", b"signature"]):
                with patch.object(repository, "run_tool", return_value=signature_status(primary=OTHER_FINGERPRINT)):
                    with patch.object(repository, "inspect_database") as inspect:
                        with self.assertRaises(repository.SetupError):
                            repository.verify_repository(workspace, workspace / "public.gpg")
            inspect.assert_not_called()

    def test_verified_repository_returns_the_database_version(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with patch.object(repository, "download", side_effect=[database(), b"signature"]):
                with patch.object(repository, "run_tool", return_value=signature_status()) as run:
                    self.assertEqual(repository.verify_repository(workspace, workspace / "public.gpg"), "0.4.0-1")
            self.assertEqual(run.call_args.args[0], "gpgv")
            self.assertIn("--status-fd", run.call_args.args)


class FakeResponse(io.BytesIO):
    def __init__(self, content=b"metadata", *, url=None, status=200, length=None):
        super().__init__(content)
        self.url = url or repository.SERVER + "/smarttar.db"
        self.status = status
        self.headers = {} if length is None else {"Content-Length": length}
        self.read_sizes = []

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class DownloadTests(unittest.TestCase):
    def download(self, response, maximum=64):
        opener = SimpleNamespace(open=lambda *args, **kwargs: response)
        with patch.object(repository.urllib.request, "build_opener", return_value=opener):
            return repository.download("smarttar.db", maximum)

    def test_download_read_is_bounded_even_without_content_length(self):
        response = FakeResponse(b"metadata")
        self.assertEqual(self.download(response), b"metadata")
        self.assertEqual(response.read_sizes, [65])

    def test_empty_and_oversized_bodies_are_rejected(self):
        for content in (b"", b"x" * 65):
            with self.subTest(size=len(content)):
                with self.assertRaises(repository.SetupError):
                    self.download(FakeResponse(content))

    def test_oversized_and_malformed_content_lengths_are_rejected_before_read(self):
        for length in ("65", "-1", "abc", "9" * 1000):
            response = FakeResponse(length=length)
            with self.subTest(length=length[:30]):
                with self.assertRaises(repository.SetupError):
                    self.download(response)
            self.assertEqual(response.read_sizes, [])

    def test_unexpected_urls_and_status_codes_are_rejected(self):
        for response in (FakeResponse(url="https://example.invalid/smarttar.db"), FakeResponse(status=404)):
            with self.assertRaises(repository.SetupError):
                self.download(response)

    def test_only_expected_metadata_names_can_be_requested(self):
        with patch.object(repository.urllib.request, "build_opener") as opener:
            with self.assertRaises(repository.SetupError):
                repository.download("../other", 64)
        opener.assert_not_called()

    def test_url_policy_rejects_other_protocols_hosts_queries_and_fragments(self):
        self.assertTrue(repository.allowed_url(repository.SERVER + "/smarttar.db"))
        for url in (
            repository.SERVER.replace("https:", "http:") + "/smarttar.db",
            "https://raw.githubusercontent.com.evil.invalid/HaytamElbouhmidi/smarttar/packages/any/smarttar.db",
            "https://user@raw.githubusercontent.com/HaytamElbouhmidi/smarttar/packages/any/smarttar.db",
            repository.SERVER + "/smarttar.db?query=1", repository.SERVER + "/smarttar.db#fragment",
        ):
            with self.subTest(url=url):
                self.assertFalse(repository.allowed_url(url))

    def test_redirect_to_another_repository_is_refused(self):
        request = urllib.request.Request(repository.SERVER + "/smarttar.db")
        with self.assertRaises(repository.SetupError):
            repository.RepositoryRedirects().redirect_request(
                request, None, 302, "Moved", {}, "https://raw.githubusercontent.com/other/project/main/file",
            )

    def test_network_errors_become_controlled_setup_errors(self):
        opener = SimpleNamespace(open=lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")))
        with patch.object(repository.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(repository.SetupError, "Check your connection"):
                repository.download("smarttar.db", 64)


class ConfigurationTests(unittest.TestCase):
    def test_append_preserves_existing_configuration_bytes(self):
        for original in (b"[options]\nArchitecture = auto\n", b"# comment without final newline"):
            with self.subTest(original=original):
                configured = repository.config_with_block(original)
                self.assertTrue(configured.startswith(original))
                self.assertEqual(configured.count(repository.BLOCK), 1)
                self.assertEqual(repository.config_with_block(configured), configured)

    def test_managed_block_removal_keeps_other_repositories_and_comments(self):
        original = b"# Custom configuration\n[core]\nServer = https://example.invalid\n"
        configured = repository.config_with_block(original)
        remainder, managed = repository.configuration_without_block(configured)
        self.assertTrue(managed)
        self.assertEqual(remainder.rstrip(b"\n"), original.rstrip(b"\n"))

    def test_customized_duplicate_incomplete_and_unmanaged_blocks_are_rejected(self):
        values = (
            repository.BLOCK.replace(b"DatabaseRequired", b"DatabaseOptional"), repository.BLOCK * 2,
            repository.BEGIN.encode(), repository.END.encode(), b"[smarttar]\nServer = https://example.invalid\n",
            repository.BLOCK + b"[smarttar] # second definition\nServer = https://example.invalid\n", b"\xff",
        )
        for content in values:
            with self.subTest(content=content[:80]):
                with self.assertRaises(repository.SetupError):
                    repository.config_with_block(content)

    def test_effective_included_repository_conflict_is_rejected(self):
        with patch.object(repository, "run_tool", return_value="core\nsmarttar\n"):
            with self.assertRaisesRegex(repository.SetupError, "included file"):
                repository.check_effective_config(Path("/unused"), False)

    def test_effective_server_and_expanded_signature_policy_are_enforced(self):
        valid = ["core\nsmarttar\n", repository.SERVER + "\n", "PackageRequired PackageTrustedOnly DatabaseRequired DatabaseTrustedOnly\n"]
        with patch.object(repository, "run_tool", side_effect=valid):
            repository.check_effective_config(Path("/unused"), True)
        for values in (
            [valid[0], "https://example.invalid\n", valid[2]],
            [valid[0], valid[1], "PackageRequired PackageTrustedOnly DatabaseOptional DatabaseTrustedOnly\n"],
        ):
            with self.subTest(values=values):
                with patch.object(repository, "run_tool", side_effect=values):
                    with self.assertRaises(repository.SetupError):
                        repository.check_effective_config(Path("/unused"), True)


class LocalFileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-config-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "pacman.conf"
        self.original = b"[options]\nArchitecture = auto\n"
        self.path.write_bytes(self.original)
        self.path.chmod(0o640)
        self.owner = os.getuid()

    def test_regular_read_rejects_symlinks_directories_and_large_files(self):
        link = self.root / "link"
        link.symlink_to(self.path)
        for path, maximum in ((link, 100), (self.root, 100), (self.path, 1)):
            with self.subTest(path=path, maximum=maximum):
                with self.assertRaises(repository.SetupError):
                    repository.read_regular(path, maximum)

    def test_regular_read_rejects_unsafe_owner_or_writable_mode(self):
        with self.assertRaises(repository.SetupError):
            repository.read_regular(self.path, 100, owner=self.owner + 1)
        self.path.chmod(0o666)
        with self.assertRaises(repository.SetupError):
            repository.read_regular(self.path, 100, owner=self.owner)

    def test_atomic_write_preserves_mode_and_replaces_complete_contents(self):
        original, details = repository.read_regular(self.path, 100, owner=self.owner)
        repository.atomic_config_write(self.path, original, original + b"new line\n", details, owner=self.owner)
        self.assertEqual(self.path.read_bytes(), original + b"new line\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(list(self.root.glob(".smarttar-pacman-*")), [])

    def test_atomic_write_rejects_changed_contents_inode_or_mode(self):
        for mutation in ("contents", "inode", "mode"):
            with self.subTest(mutation=mutation):
                self.path.write_bytes(self.original)
                self.path.chmod(0o640)
                original, details = repository.read_regular(self.path, 100, owner=self.owner)
                if mutation == "contents":
                    self.path.write_bytes(b"changed content\n")
                elif mutation == "inode":
                    replacement = self.root / "replacement"
                    replacement.write_bytes(original)
                    replacement.chmod(0o640)
                    replacement.replace(self.path)
                else:
                    self.path.chmod(0o600)
                current = self.path.read_bytes()
                mode = self.path.stat().st_mode
                with self.assertRaises(repository.SetupError):
                    repository.atomic_config_write(self.path, original, b"bad overwrite", details, owner=self.owner)
                self.assertEqual(self.path.read_bytes(), current)
                self.assertEqual(self.path.stat().st_mode, mode)

    def test_second_atomic_check_preserves_concurrent_permission_change(self):
        original, details = repository.read_regular(self.path, 100, owner=self.owner)
        mkstemp = repository.tempfile.mkstemp

        def changed_permissions(*args, **kwargs):
            result = mkstemp(*args, **kwargs)
            self.path.chmod(0o600)
            return result

        with patch.object(repository.tempfile, "mkstemp", side_effect=changed_permissions):
            with self.assertRaises(repository.SetupError):
                repository.atomic_config_write(self.path, original, b"bad overwrite", details, owner=self.owner)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_failed_atomic_replace_preserves_original_and_cleans_temporary_file(self):
        original, details = repository.read_regular(self.path, 100, owner=self.owner)
        with patch.object(repository.os, "replace", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaises(OSError):
                repository.atomic_config_write(self.path, original, b"replacement", details, owner=self.owner)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".smarttar-pacman-*")), [])

    def test_backup_is_exclusive_private_and_exact(self):
        destination = self.root / "backups"
        destination.mkdir()
        with patch.object(repository, "secure_directory") as secure:
            first = repository.backup_config(self.original, directory=destination, owner=self.owner)
            second = repository.backup_config(self.original, directory=destination, owner=self.owner)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        secure.assert_called_with(destination, owner=self.owner)


class SecureDirectoryTests(unittest.TestCase):
    def virtual_check(self, unsafe=None, *, missing=None):
        checked = []
        made = []
        paths = {Path("/"), Path("/trusted"), Path("/trusted/parent"), Path("/trusted/parent/backups")}
        if missing:
            paths.remove(Path(missing))

        def exists(path):
            return path in paths

        def metadata(path):
            checked.append(path)
            mode = stat.S_IFDIR | 0o755
            owner = 0
            if path == unsafe:
                mode = stat.S_IFDIR | 0o777
            return SimpleNamespace(st_mode=mode, st_uid=owner)

        def mkdir(path, mode):
            made.append((path, mode))
            paths.add(path)

        with patch.object(Path, "exists", exists), patch.object(Path, "is_symlink", return_value=False):
            with patch.object(Path, "lstat", metadata), patch.object(Path, "mkdir", mkdir):
                repository.secure_directory(Path("/trusted/parent/backups"))
        return checked, made

    def test_existing_directory_still_validates_all_ancestors(self):
        checked, made = self.virtual_check()
        self.assertEqual(checked, [Path("/"), Path("/trusted"), Path("/trusted/parent"), Path("/trusted/parent/backups")])
        self.assertEqual(made, [])

    def test_writable_ancestor_is_rejected(self):
        with self.assertRaises(repository.SetupError):
            self.virtual_check(unsafe=Path("/trusted"))

    def test_missing_leaf_is_created_only_after_ancestors_are_valid(self):
        checked, made = self.virtual_check(missing="/trusted/parent/backups")
        self.assertEqual(made, [(Path("/trusted/parent/backups"), 0o755)])
        self.assertEqual(checked[0], Path("/"))

    def test_relative_directory_is_rejected(self):
        with self.assertRaises(repository.SetupError):
            repository.secure_directory(Path("relative/backups"))


class RepositoryTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-bootstrap-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "pacman.conf"
        self.original = b"# Existing configuration\n[options]\nArchitecture = auto\n"
        self.config.write_bytes(self.original)
        self.config.chmod(0o644)
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.key = self.root / "public.asc"
        self.key.write_bytes(b"Public fixture, never imported.\n")
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.read_regular = repository.read_regular
        self.backup_config = repository.backup_config

        def owned_read(path, maximum, *, owner=None):
            return self.read_regular(path, maximum, owner=os.getuid() if owner == 0 else owner)

        def private_backup(content):
            with patch.object(repository, "secure_directory"):
                return self.backup_config(content, self.backups, owner=os.getuid())

        self.start_patch(patch.object(repository, "read_regular", side_effect=owned_read))
        self.backup = self.start_patch(patch.object(repository, "backup_config", side_effect=private_backup))
        self.prepare = self.start_patch(patch.object(repository, "prepare_public_key", return_value=(self.key, self.root / "public.gpg")))
        self.verify = self.start_patch(patch.object(repository, "verify_repository", return_value="0.4.0-1"))
        self.effective = self.start_patch(patch.object(repository, "check_effective_config"))
        self.confirm = self.start_patch(patch.object(repository, "confirm", return_value=True))
        self.run = self.start_patch(patch.object(repository, "run_tool", return_value="smarttar 0.4.0-1\n"))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def install(self):
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            repository.install_repository(self.key, self.config)

    def fail_pacman(self, *args, **kwargs):
        if args[0] == "pacman":
            raise repository.SetupError("Synthetic Pacman failure")
        return ""

    def test_success_backs_up_exact_config_and_uses_full_interactive_system_update(self):
        self.install()
        self.assertEqual(self.config.read_bytes(), repository.config_with_block(self.original))
        backups = list(self.backups.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertIn(call("pacman", "--config", self.config, "-Syu", "--needed", "smarttar/smarttar", capture=False, timeout=None), self.run.call_args_list)
        self.assertNotIn("--noconfirm", str(self.run.call_args_list))
        self.assertIn("not an official Arch or CachyOS repository", self.stdout.getvalue())

    def test_cancel_before_confirmation_changes_no_config_or_keyring(self):
        self.confirm.return_value = False
        with self.assertRaisesRegex(repository.SetupError, "cancelled"):
            self.install()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.backup.assert_not_called()
        self.run.assert_not_called()

    def test_public_key_or_repository_verification_failure_changes_nothing(self):
        for operation in (self.prepare, self.verify):
            with self.subTest(operation=operation):
                operation.side_effect = repository.SetupError("Synthetic verification failure")
                with self.assertRaises(repository.SetupError):
                    self.install()
                operation.side_effect = None
                self.assertEqual(self.config.read_bytes(), self.original)
                self.run.assert_not_called()
                self.backup.assert_not_called()

    def test_keyring_failure_does_not_change_pacman_configuration(self):
        self.run.side_effect = repository.SetupError("Synthetic public-key import failure")
        with self.assertRaises(repository.SetupError):
            self.install()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertIn("Any public signing key imported by this setup remains", self.stderr.getvalue())

    def test_new_effective_config_conflict_rolls_back_the_managed_block(self):
        self.effective.side_effect = [None, repository.SetupError("Synthetic included-file conflict")]
        with self.assertRaises(repository.SetupError):
            self.install()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(any(item.args[0] == "pacman" for item in self.run.call_args_list))

    def test_configuration_changed_during_confirmation_is_preserved(self):
        latest = self.original + b"# Independent change during confirmation\n"

        def changed_config(*args, **kwargs):
            self.config.write_bytes(latest)
            return True

        self.confirm.side_effect = changed_config
        with self.assertRaises(repository.SetupError):
            self.install()
        self.assertEqual(self.config.read_bytes(), latest)
        self.assertFalse(any(item.args[0] == "pacman" for item in self.run.call_args_list))

    def test_pacman_failure_restores_only_the_newly_added_block(self):
        self.run.side_effect = self.fail_pacman
        with self.assertRaises(repository.SetupError):
            self.install()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertIn("settings were rolled back", self.stderr.getvalue())
        self.assertIn("public signing key imported by this setup remains", self.stderr.getvalue())

    def test_keyboard_cancellation_during_pacman_restores_the_original_config(self):
        def cancel(*args, **kwargs):
            if args[0] == "pacman":
                raise KeyboardInterrupt()
            return ""
        self.run.side_effect = cancel
        with self.assertRaises(KeyboardInterrupt):
            self.install()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_preexisting_managed_block_is_kept_when_pacman_fails(self):
        managed = repository.config_with_block(self.original)
        self.config.write_bytes(managed)
        self.run.side_effect = self.fail_pacman
        with self.assertRaises(repository.SetupError):
            self.install()
        self.assertEqual(self.config.read_bytes(), managed)
        self.backup.assert_not_called()

    def test_later_configuration_edits_are_not_overwritten_on_failure(self):
        later = repository.config_with_block(self.original) + b"\n# Changed independently\n"

        def changed_config(*args, **kwargs):
            if args[0] == "pacman":
                self.config.write_bytes(later)
                raise repository.SetupError("Synthetic Pacman failure")
            return ""
        self.run.side_effect = changed_config
        with self.assertRaises(repository.SetupError):
            self.install()
        self.assertEqual(self.config.read_bytes(), later)
        self.assertIn("changed separately", self.stderr.getvalue())

    def test_error_after_atomic_replace_still_restores_original_configuration(self):
        atomic = repository.atomic_config_write
        calls = []

        def post_replace_failure(*args, **kwargs):
            atomic(*args, **kwargs)
            calls.append(args)
            if len(calls) == 1:
                raise OSError("Synthetic directory fsync failure")

        with patch.object(repository, "atomic_config_write", side_effect=post_replace_failure):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(len(calls), 2)
        self.assertIn("settings were rolled back", self.stderr.getvalue())

    def test_failed_rollback_keeps_a_recoverable_backup_and_reports_it(self):
        atomic = repository.atomic_config_write
        count = 0

        def failed_recovery(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                atomic(*args, **kwargs)
            raise OSError("Synthetic disk failure")

        with patch.object(repository, "atomic_config_write", side_effect=failed_recovery):
            with self.assertRaises(OSError):
                self.install()
        backups = list(self.backups.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertIn(str(backups[0]), self.stderr.getvalue())
        self.assertIn("recovery could not finish", self.stderr.getvalue())

    def test_removal_preserves_unrelated_configuration_and_keeps_packages_and_keyring(self):
        self.config.write_bytes(repository.config_with_block(self.original))
        with redirect_stdout(self.stdout):
            repository.remove_repository(self.config)
        self.assertEqual(self.config.read_bytes().rstrip(b"\n"), self.original.rstrip(b"\n"))
        self.run.assert_not_called()
        self.assertIn("package and signing key were left in place", self.stdout.getvalue())

    def test_removal_cancellation_changes_nothing(self):
        managed = repository.config_with_block(self.original)
        self.config.write_bytes(managed)
        self.confirm.return_value = False
        with redirect_stdout(self.stdout):
            with self.assertRaises(repository.SetupError):
                repository.remove_repository(self.config)
        self.assertEqual(self.config.read_bytes(), managed)
        self.backup.assert_not_called()


class ProcessBoundaryTests(unittest.TestCase):
    def test_nonroot_main_stops_before_filesystem_lock_or_system_commands(self):
        with patch.object(sys, "argv", ["configure_repository.py", "--key", "/unused-public.asc"]):
            with patch.object(repository.os, "geteuid", return_value=1000):
                with patch.object(repository, "run_tool") as run, patch.object(repository, "setup_lock") as lock:
                    with redirect_stderr(io.StringIO()):
                        self.assertEqual(repository.main(), 1)
        run.assert_not_called()
        lock.assert_not_called()

    def test_command_environment_is_minimal_and_executable_path_is_fixed(self):
        response = subprocess.CompletedProcess([], 0, "output", "")
        with patch.object(repository.subprocess, "run", return_value=response) as run:
            self.assertEqual(repository.run_tool("pacman-conf", "--repo-list"), "output")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/pacman-conf", "--repo-list"])
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("GPG_AGENT_INFO", environment)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_failed_and_timed_out_commands_become_controlled_errors(self):
        with patch.object(repository.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "failure")):
            with self.assertRaises(repository.SetupError):
                repository.run_tool("pacman", "-Q", "smarttar")
        with patch.object(repository.subprocess, "run", side_effect=subprocess.TimeoutExpired("pacman", 90)):
            with self.assertRaises(repository.SetupError):
                repository.run_tool("pacman", "-Q", "smarttar")

    def test_confirmation_is_explicit_and_interrupt_safe(self):
        for answer, expected in (("y", True), (" YES ", True), ("", False), ("n", False), ("maybe", False)):
            with self.subTest(answer=answer):
                with patch("builtins.input", return_value=answer):
                    self.assertEqual(repository.confirm("Continue?"), expected)
        for exception in (EOFError(), KeyboardInterrupt()):
            with patch("builtins.input", side_effect=exception):
                self.assertFalse(repository.confirm("Continue?"))


class SetupLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="smarttar-lock-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lock = self.root / "setup.lock"

    def test_exclusive_private_fixture_lock_closes_its_descriptor(self):
        descriptors = []
        fstat = repository.os.fstat

        def simulated_root_ownership(descriptor):
            details = fstat(descriptor)
            descriptors.append(descriptor)
            return SimpleNamespace(st_mode=details.st_mode, st_uid=0)

        with patch.object(repository.os, "fstat", side_effect=simulated_root_ownership):
            with repository.setup_lock(self.lock):
                self.assertTrue(self.lock.is_file())
        with self.assertRaises(OSError):
            fstat(descriptors[0])
        self.assertEqual(stat.S_IMODE(self.lock.stat().st_mode), 0o600)

    def test_untrusted_owner_is_rejected(self):
        details = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1)
        with patch.object(repository.os, "fstat", return_value=details):
            with self.assertRaises(repository.SetupError):
                with repository.setup_lock(self.lock):
                    self.fail("An untrusted lock must not be acquired.")

    def test_writable_lock_is_rejected(self):
        details = SimpleNamespace(st_mode=stat.S_IFREG | 0o666, st_uid=0)
        with patch.object(repository.os, "fstat", return_value=details):
            with self.assertRaises(repository.SetupError):
                with repository.setup_lock(self.lock):
                    self.fail("A writable lock must not be acquired.")

    def test_symlinked_lock_is_not_followed(self):
        outside = self.root / "other-file"
        outside.write_bytes(b"Keep this file unchanged.\n")
        self.lock.symlink_to(outside)
        with self.assertRaises(OSError):
            with repository.setup_lock(self.lock):
                self.fail("A symlinked lock must not be acquired.")
        self.assertEqual(outside.read_bytes(), b"Keep this file unchanged.\n")

    def test_lock_contention_is_reported_without_waiting(self):
        details = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0)
        with patch.object(repository.os, "fstat", return_value=details):
            with patch.object(repository.fcntl, "flock", side_effect=BlockingIOError("busy")):
                with self.assertRaisesRegex(repository.SetupError, "already running"):
                    with repository.setup_lock(self.lock):
                        self.fail("A busy lock must not be acquired.")


class PublicOnlyIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(Path("/usr/bin/gpg").is_file() and (PROJECT / "packaging/keys/smarttar.asc").is_file(), "Requires GnuPG and bundled public key")
    def test_real_public_key_listing_and_dearmor_use_an_isolated_directory(self):
        with tempfile.TemporaryDirectory(prefix="smarttar-public-key-test-") as directory:
            workspace = Path(directory)
            source = PROJECT / "packaging/keys/smarttar.asc"
            snapshot, keyring = repository.prepare_public_key(source, workspace)
            self.assertEqual(snapshot.read_bytes(), source.read_bytes())
            self.assertTrue(keyring.is_file())
            self.assertGreater(keyring.stat().st_size, 0)
            self.assertEqual(list((workspace / "private-keys-v1.d").glob("*")), [])

    @unittest.skipUnless(Path("/usr/bin/pacman-conf").is_file(), "Requires Arch pacman-conf")
    def test_real_pacman_conf_expands_server_and_signature_policy_without_network(self):
        with tempfile.TemporaryDirectory(prefix="smarttar-pacman-conf-test-") as directory:
            config = Path(directory) / "pacman.conf"
            config.write_bytes(b"[options]\nArchitecture = auto\n" + repository.BLOCK)
            repository.check_effective_config(config, True)


if __name__ == "__main__":
    unittest.main()
