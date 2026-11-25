#!/usr/bin/env python3
"""SmartTar v0.4.0 signed package publication and public-download verification."""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request


VERSION = "0.4.0"
REPOSITORY = "HaytamElbouhmidi/smarttar"
SERVER = "https://raw.githubusercontent.com/HaytamElbouhmidi/smarttar/packages/any"
SIGNING_FINGERPRINT = "89EFC028E6A2EF984615453D47B7FD7E5F99031F"
SEMVER = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
PACKAGE_NAME = re.compile(rf"smarttar-({SEMVER})-([1-9][0-9]*)-any\.pkg\.tar\.zst")
MAX_DATABASE_BYTES = 4 * 1024 * 1024
MAX_DATABASE_EXPANDED_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_BYTES = 32 * 1024 * 1024


class PublicationError(Exception):
    pass


def command(arguments, *, label, data=None, environment=None, timeout=120):
    try:
        result = subprocess.run(arguments, input=data, capture_output=True, env=environment, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicationError(f"{label} could not complete.") from exc
    if result.returncode:
        # Tool output can contain secret-import diagnostics, so it is not logged.
        raise PublicationError(f"{label} failed with exit status {result.returncode}.")
    return result.stdout


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def regular(path, maximum):
    try:
        details = path.lstat()
    except OSError as exc:
        raise PublicationError(f"Required file is missing: {path.name}") from exc
    if not stat.S_ISREG(details.st_mode) or not 0 < details.st_size <= maximum:
        raise PublicationError(f"File is not a bounded regular file: {path.name}")


def load_json(path):
    regular(path, 64 * 1024)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise PublicationError(f"Invalid JSON file: {path.name}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"Expected a JSON object: {path.name}")
    return value


def write_json(path, value):
    if path.exists() or path.is_symlink():
        raise PublicationError(f"Output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")


def release_tag(tag):
    if not isinstance(tag, str) or not re.fullmatch("v" + SEMVER, tag):
        raise PublicationError("Choose an exact stable release tag such as v0.4.0.")
    return tag[1:]


def read_version(source):
    try:
        tree = ast.parse(source)
        values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
                  and isinstance(node.value, ast.Constant)]
    except (SyntaxError, ValueError) as exc:
        raise PublicationError("The release source could not be inspected.") from exc
    if len(values) != 1 or not isinstance(values[0], str):
        raise PublicationError("The release source has no unique literal version.")
    return values[0]


def validate_release(repository, tag, output):
    version = release_tag(tag)
    git = ["git", "-C", str(repository)]
    remote = command(git + ["remote", "get-url", "origin"], label="Repository identity check").decode().strip().removesuffix(".git")
    if remote != "https://github.com/" + REPOSITORY:
        raise PublicationError("Release validation requires the official SmartTar repository.")
    head = command(git + ["rev-parse", "HEAD"], label="Control checkout check").decode().strip()
    main = command(git + ["rev-parse", "refs/remotes/origin/main"], label="Trusted main lookup").decode().strip()
    if head != main:
        raise PublicationError("The control checkout must be the fetched main branch.")
    commit = command(git + ["rev-parse", "--verify", "refs/tags/" + tag + "^{commit}"], label="Release tag lookup").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PublicationError("The release tag did not resolve to a commit.")
    command(git + ["merge-base", "--is-ancestor", commit, main], label="Trusted main ancestry check")
    source = command(git + ["show", commit + ":smarttar.py"], label="Release source inspection").decode("utf-8")
    if read_version(source) != version:
        raise PublicationError("The release tag and application version do not match.")
    recipe = command(git + ["show", commit + ":packaging/arch/PKGBUILD.in"], label="Package recipe inspection").decode("utf-8")
    recipe_version = re.findall(r"^pkgver=(\S+)$", recipe, flags=re.MULTILINE)
    package_release = re.findall(r"^pkgrel=([1-9][0-9]*)$", recipe, flags=re.MULTILINE)
    if recipe_version != [version] or len(package_release) != 1:
        raise PublicationError("The release tag and package recipe do not match.")
    record = {
        "schema_version": 1, "repository": REPOSITORY, "tag": tag, "version": version,
        "package_release": int(package_release[0]), "source_commit": commit,
        "trusted_main_commit": main, "signing_fingerprint": SIGNING_FINGERPRINT,
    }
    write_json(output, record)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as stream:
            stream.write(f"commit={commit}\nversion={version}\npackage_release={package_release[0]}\n")
    print(f"Validated {tag} at {commit} against trusted main.")


def verify_release_record(path):
    record = load_json(path)
    version = release_tag(record.get("tag", ""))
    if record.get("schema_version") != 1 or record.get("version") != version or record.get("repository") != REPOSITORY:
        raise PublicationError("The verified release record has an unexpected identity.")
    if record.get("signing_fingerprint") != SIGNING_FINGERPRINT:
        raise PublicationError("The verified release record names an unexpected signing key.")
    if not isinstance(record.get("source_commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", record["source_commit"]):
        raise PublicationError("The verified release record has an invalid source commit.")
    if not isinstance(record.get("trusted_main_commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", record["trusted_main_commit"]):
        raise PublicationError("The verified release record has an invalid trusted main commit.")
    if type(record.get("package_release")) is not int or record["package_release"] < 1:
        raise PublicationError("The verified release record has an invalid package release.")
    return record


def validate_package_evidence(package, test_report, release):
    regular(package, MAX_PACKAGE_BYTES)
    expected_name = f"smarttar-{release['version']}-{release['package_release']}-any.pkg.tar.zst"
    if package.name != expected_name:
        raise PublicationError("The built package filename does not match the verified release.")
    sha256 = digest(package)
    tests = load_json(test_report)
    required_checks = (
        "package_layout_verified", "package_signature_verified", "repository_database_signature_verified",
        "tampered_package_rejected", "unsigned_package_rejected", "tampered_database_rejected",
        "existing_apps_shortcuts_and_preferences_preserved",
    )
    if tests.get("status") != "passed" or tests.get("package_sha256") != sha256:
        raise PublicationError("The package has not passed the matching signed-upgrade test.")
    if tests.get("version") != release["version"] or tests.get("package_release") != release["package_release"]:
        raise PublicationError("The test report does not match the release version.")
    if not all(tests.get(check) is True for check in required_checks):
        raise PublicationError("The package test report is missing a required passing check.")
    upgrade = tests.get("upgrade", {})
    if not isinstance(upgrade, dict) or upgrade.get("to") != f"{release['version']}-{release['package_release']}" or upgrade.get("isolated_root") is not True or upgrade.get("command") != "pacman -Syu":
        raise PublicationError("A successful isolated package upgrade was not recorded.")
    if tests.get("production_signing_key_used") is not False:
        raise PublicationError("Package tests must use only a disposable signing key.")
    build = load_json(package.parent / "package-build.json")
    if build.get("package_sha256") != sha256 or build.get("version") != release["version"] or build.get("package_release") != release["package_release"] or build.get("architecture") != "any" or build.get("package") != package.name:
        raise PublicationError("The build record does not match the package.")
    if not isinstance(build.get("source_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", build["source_sha256"]):
        raise PublicationError("The build record has no valid source checksum.")
    return sha256, build["source_sha256"]


def primary_fingerprints(colon_text, record_type):
    result = []
    expect_primary = False
    for line in colon_text.splitlines():
        fields = line.split(":")
        if fields[0] == record_type:
            expect_primary = True
        elif fields[0] in {"pub", "sub", "sec", "ssb"}:
            expect_primary = False
        elif fields[0] == "fpr" and expect_primary:
            if len(fields) > 9:
                result.append(fields[9])
            expect_primary = False
    return result


@contextmanager
def verification_key(public_key):
    regular(public_key, 64 * 1024)
    if b"PRIVATE KEY" in public_key.read_bytes():
        raise PublicationError("Only a public signing key is allowed in release source.")
    with tempfile.TemporaryDirectory(prefix="smarttar-public-key-") as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        listing = command(["gpg", "--no-options", "--no-autostart", "--homedir", str(directory), "--batch", "--with-colons", "--show-keys", str(public_key)], label="Public signing-key inspection").decode()
        if primary_fingerprints(listing, "pub") != [SIGNING_FINGERPRINT]:
            raise PublicationError("The public signing key does not match the pinned fingerprint.")
        keyring = directory / "trustedkeys.gpg"
        command(["gpg", "--no-options", "--no-autostart", "--homedir", str(directory), "--batch", "--yes", "--output", str(keyring), "--dearmor", str(public_key)], label="Public verification-key preparation")
        yield keyring


def verify_signature(path, keyring):
    regular(path, MAX_PACKAGE_BYTES)
    signature = Path(str(path) + ".sig")
    regular(signature, 64 * 1024)
    status = command(["gpgv", "--homedir", str(keyring.parent), "--keyring", str(keyring), "--status-fd", "1", str(signature), str(path)], label=f"Signature verification for {path.name}").decode()
    records = [line.split() for line in status.splitlines() if line.startswith("[GNUPG:] ")]
    errors = {"BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG", "KEYREVOKED", "KEYEXPIRED", "SIGEXPIRED", "FAILURE", "ERROR", "NO_PUBKEY", "NODATA"}
    if any(len(record) > 1 and record[1] in errors for record in records):
        raise PublicationError(f"Invalid or expired signature for {path.name}")
    valid = [record for record in records if len(record) > 1 and record[1] == "VALIDSIG"]
    if len(valid) != 1 or len(valid[0]) != 12 or valid[0][11] != SIGNING_FINGERPRINT or not re.fullmatch(r"[A-F0-9]{40}", valid[0][2]):
        raise PublicationError(f"Unexpected signer for {path.name}")


def database_entry(database):
    regular(database, MAX_DATABASE_BYTES)
    descriptions = []
    try:
        with gzip.open(database, "rb") as compressed:
            expanded = compressed.read(MAX_DATABASE_EXPANDED_BYTES + 1)
        if len(expanded) > MAX_DATABASE_EXPANDED_BYTES:
            raise PublicationError("The expanded repository database exceeds its expected size.")
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            count = 0
            for member in archive:
                count += 1
                if count > 16 or member.size > 128 * 1024:
                    raise PublicationError("Repository database exceeds its expected bounds.")
                if member.isdir():
                    continue
                if not member.isfile() or not re.fullmatch(r"smarttar-[^/]+/desc", member.name):
                    raise PublicationError("The repository database contains an unexpected entry.")
                source = archive.extractfile(member)
                if source is None:
                    raise PublicationError("The repository description is missing.")
                descriptions.append(source.read(128 * 1024 + 1).decode("utf-8"))
    except (tarfile.TarError, UnicodeError, OSError, EOFError) as exc:
        raise PublicationError("The signed repository database could not be read.") from exc
    if len(descriptions) != 1:
        raise PublicationError("The repository must contain exactly one current SmartTar package.")
    fields = {}
    key = None
    for line in descriptions[0].splitlines():
        if line.startswith("%") and line.endswith("%"):
            key = line[1:-1]
            if key in fields:
                raise PublicationError("The database contains duplicate description fields.")
            fields[key] = []
        elif line and key:
            fields[key].append(line)
    for key in ("NAME", "VERSION", "ARCH", "FILENAME", "SHA256SUM"):
        if len(fields.get(key, [])) != 1:
            raise PublicationError(f"The repository has no unique {key} field.")
    entry = {key: values[0] for key, values in fields.items() if len(values) == 1}
    if entry["NAME"] != "smarttar" or entry["ARCH"] != "any":
        raise PublicationError("The repository describes an unexpected package.")
    match = PACKAGE_NAME.fullmatch(entry["FILENAME"])
    if not match or entry["VERSION"] != f"{match[1]}-{match[2]}" or not re.fullmatch(r"[0-9a-f]{64}", entry["SHA256SUM"]):
        raise PublicationError("The repository package filename, version, or hash is invalid.")
    return entry


def validate_distribution_tree(repository):
    if repository.is_symlink() or not repository.is_dir():
        raise PublicationError("The distribution checkout must be a normal directory.")
    for child in repository.iterdir():
        if child.name == ".git":
            if child.is_symlink() or not child.is_dir():
                raise PublicationError("Unexpected distribution Git directory.")
        elif child.name == "README.md":
            regular(child, 64 * 1024)
        elif child.name == "any":
            if child.is_symlink() or not child.is_dir():
                raise PublicationError("The distribution architecture directory is invalid.")
            for artifact in child.iterdir():
                if not valid_artifact_name(artifact.name):
                    raise PublicationError(f"Unexpected distribution artifact: {artifact.name}")
                regular(artifact, MAX_PACKAGE_BYTES)
        else:
            raise PublicationError(f"Unexpected distribution path: {child.name}")


def valid_artifact_name(name):
    fixed = {"smarttar.asc", "release.json", "release.json.sig"}
    if name in fixed or re.fullmatch(r"smarttar\.(?:db|files)(?:\.tar\.gz)?(?:\.sig)?", name):
        return True
    for suffix in ("", ".sig", ".release.json", ".release.json.sig"):
        candidate = name[:-len(suffix)] if suffix else name
        if (not suffix or name.endswith(suffix)) and PACKAGE_NAME.fullmatch(candidate):
            return True
    return False


def sign_file(path, signing_home):
    command(["gpg", "--no-options", "--homedir", str(signing_home), "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase", "", "--local-user", SIGNING_FINGERPRINT, "--output", str(path) + ".sig", "--detach-sign", str(path)], label=f"Signing {path.name}")


def publish(package, test_report, public_key, verified_release, repository, result_path):
    secret = os.environ.pop("SMARTTAR_SIGNING_KEY", "")
    if not secret or len(secret) > 128 * 1024:
        raise PublicationError("The dedicated SMARTTAR_SIGNING_KEY secret is missing or invalid.")
    release = verify_release_record(verified_release)
    package_sha256, source_sha256 = validate_package_evidence(package, test_report, release)
    validate_distribution_tree(repository)
    artifacts = repository / "any"
    artifacts.mkdir(exist_ok=True)
    with verification_key(public_key) as keyring:
        current = artifacts / "smarttar.db"
        if current.exists():
            verify_signature(current, keyring)
            compressed_current = artifacts / "smarttar.db.tar.gz"
            verify_signature(compressed_current, keyring)
            if digest(current) != digest(compressed_current):
                raise PublicationError("The current database alias does not match the signed database archive.")
            prior = database_entry(current)
            previous_version = tuple(int(part) for part in re.split(r"[.-]", prior["VERSION"]))
            next_version = tuple(int(part) for part in re.split(r"[.-]", f"{release['version']}-{release['package_release']}"))
            if previous_version > next_version:
                raise PublicationError("Publishing an older package would downgrade the repository.")
            prior_package = artifacts / prior["FILENAME"]
            verify_signature(prior_package, keyring)
            if digest(prior_package) != prior["SHA256SUM"]:
                raise PublicationError("The retained package does not match its signed database.")
            if previous_version == next_version:
                prior_record = Path(str(prior_package) + ".release.json")
                verify_signature(prior_record, keyring)
                record = load_json(prior_record)
                if record.get("source_commit") != release["source_commit"] or record.get("source_sha256") != source_sha256 or record.get("package_sha256") != prior["SHA256SUM"]:
                    raise PublicationError("This package version was already published from different source. Increase its version or package release.")
                write_json(result_path, {"status": "already_current", "version": release["version"], "package_release": release["package_release"], "source_commit": release["source_commit"], "package_sha256": prior["SHA256SUM"]})
                print("The requested signed release is already current. No package was replaced.")
                return
        elif any(artifacts.iterdir()):
            raise PublicationError("The existing distribution has artifacts but no signed database.")
        destination = artifacts / package.name
        if destination.exists() or destination.is_symlink():
            raise PublicationError("A package with this filename already exists and will not be overwritten.")
        with tempfile.TemporaryDirectory(prefix="smarttar-release-signing-") as temporary:
            signing_home = Path(temporary)
            os.chmod(signing_home, 0o700)
            try:
                command(["gpg", "--no-options", "--homedir", str(signing_home), "--batch", "--quiet", "--import"], label="Dedicated release signing-key import", data=secret.encode("utf-8"))
                secret = ""
                listed = command(["gpg", "--no-options", "--homedir", str(signing_home), "--batch", "--with-colons", "--list-secret-keys"], label="Dedicated release signing-key check").decode()
                if primary_fingerprints(listed, "sec") != [SIGNING_FINGERPRINT]:
                    raise PublicationError("The private signing key does not match the pinned public key.")
                shutil.copyfile(package, destination)
                os.chmod(destination, 0o644)
                sign_file(destination, signing_home)
                verify_signature(destination, keyring)
                environment = dict(os.environ, GNUPGHOME=str(signing_home), GPGKEY=SIGNING_FINGERPRINT)
                command(["repo-add", "--sign", "--key", SIGNING_FINGERPRINT, "--include-sigs", "--prevent-downgrade", str(artifacts / "smarttar.db.tar.gz"), str(destination)], label="Signed package database creation", environment=environment)
                for database_name in ("smarttar.db", "smarttar.files"):
                    compressed = artifacts / (database_name + ".tar.gz")
                    regular(compressed, MAX_DATABASE_BYTES)
                    if not Path(str(compressed) + ".sig").is_file():
                        sign_file(compressed, signing_home)
                    verify_signature(compressed, keyring)
                    for suffix in ("", ".sig"):
                        alias = artifacts / (database_name + suffix)
                        if alias.exists() or alias.is_symlink():
                            alias.unlink()
                        shutil.copyfile(Path(str(compressed) + suffix), alias)
                        os.chmod(alias, 0o644)
                resulting = database_entry(artifacts / "smarttar.db")
                if resulting["FILENAME"] != package.name or resulting["SHA256SUM"] != package_sha256:
                    raise PublicationError("The signed database does not describe the tested package.")
                record = {
                    "schema_version": 1, "version": release["version"], "package_release": release["package_release"],
                    "tag": release["tag"], "source_commit": release["source_commit"],
                    "source_sha256": source_sha256, "package": package.name, "package_sha256": package_sha256,
                    "signing_fingerprint": SIGNING_FINGERPRINT, "published_utc": datetime.now(timezone.utc).isoformat(),
                    "package_upgrade_tests": "passed with a disposable test signing key before production signing",
                }
                history_record = Path(str(destination) + ".release.json")
                write_json(history_record, record)
                sign_file(history_record, signing_home)
                verify_signature(history_record, keyring)
                for suffix in ("", ".sig"):
                    shutil.copyfile(Path(str(history_record) + suffix), artifacts / ("release.json" + suffix))
                shutil.copyfile(public_key, artifacts / "smarttar.asc")
                for child in artifacts.iterdir():
                    if re.fullmatch(r"smarttar\.(?:db|files)(?:\.tar\.gz)?(?:\.sig)?\.old(?:\.sig)?", child.name):
                        child.unlink()
            finally:
                secret = ""
                subprocess.run(["gpgconf", "--homedir", str(signing_home), "--kill", "all"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        (repository / "README.md").write_text(
            "# SmartTar signed package repository\n\n"
            "This branch contains signed Arch Linux packages and their update catalogue.\n\n"
            "Use the installation instructions in the [SmartTar project](https://github.com/HaytamElbouhmidi/smarttar).\n"
            "Older package files are retained for clients with a cached catalogue.\n",
            encoding="utf-8",
        )
        validate_distribution_tree(repository)
        write_json(result_path, {"status": "ready", **record})
        print(f"Prepared signed SmartTar {release['version']}-{release['package_release']} for atomic publication.")


def download(url, path, maximum):
    if not url.startswith(SERVER + "/"):
        raise PublicationError("An unexpected package download source was requested.")
    request = urllib.request.Request(url, headers={"User-Agent": "SmartTar-release-check/" + VERSION, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if not response.geturl().startswith(SERVER + "/"):
                raise PublicationError("The package source redirected to an unexpected location.")
            payload = response.read(maximum + 1)
        if not 0 < len(payload) <= maximum:
            raise PublicationError("The package download exceeded its expected size.")
        path.write_bytes(payload)
    except (OSError, urllib.error.URLError) as exc:
        raise PublicationError("The public package download is not available yet.") from exc


def verify_live(public_key, verified_release, output):
    release = verify_release_record(verified_release)
    with verification_key(public_key) as keyring, tempfile.TemporaryDirectory(prefix="smarttar-live-check-") as temporary:
        directory = Path(temporary)
        for attempt in range(6):
            try:
                database = directory / "smarttar.db"
                download(SERVER + "/smarttar.db", database, MAX_DATABASE_BYTES)
                download(SERVER + "/smarttar.db.sig", Path(str(database) + ".sig"), 64 * 1024)
                verify_signature(database, keyring)
                entry = database_entry(database)
                if entry["VERSION"] != f"{release['version']}-{release['package_release']}":
                    raise PublicationError("The public catalogue has not reached the requested release.")
                package = directory / entry["FILENAME"]
                download(SERVER + "/" + package.name, package, MAX_PACKAGE_BYTES)
                download(SERVER + "/" + package.name + ".sig", Path(str(package) + ".sig"), 64 * 1024)
                verify_signature(package, keyring)
                if digest(package) != entry["SHA256SUM"]:
                    raise PublicationError("The public package hash does not match its signed catalogue.")
                manifest = directory / "release.json"
                download(SERVER + "/release.json", manifest, 64 * 1024)
                download(SERVER + "/release.json.sig", Path(str(manifest) + ".sig"), 64 * 1024)
                verify_signature(manifest, keyring)
                record = load_json(manifest)
                if record.get("source_commit") != release["source_commit"] or record.get("package_sha256") != entry["SHA256SUM"]:
                    raise PublicationError("The public signed release record does not match the requested source.")
                break
            except PublicationError:
                if attempt == 5:
                    raise
                print("Waiting for consistent signed files from the public download cache.")
                time.sleep(10)
        write_json(output, {
            "status": "passed", "version": release["version"], "package_release": release["package_release"],
            "source_commit": release["source_commit"], "server": SERVER,
            "package_sha256": entry["SHA256SUM"], "signing_fingerprint": SIGNING_FINGERPRINT,
            "https_database_signature_verified": True, "https_package_signature_verified": True,
            "https_release_manifest_signature_verified": True,
        })
        print("The public HTTPS catalogue, package, and release record have valid matching signatures.")


def main():
    parser = argparse.ArgumentParser(description=f"SmartTar v{VERSION} signed repository tools")
    commands = parser.add_subparsers(dest="operation", required=True)
    validate = commands.add_parser("validate-release")
    validate.add_argument("--repository", type=Path, required=True)
    validate.add_argument("--tag", required=True)
    validate.add_argument("--output", type=Path, required=True)
    publication = commands.add_parser("publish")
    publication.add_argument("--package", type=Path, required=True)
    publication.add_argument("--test-report", type=Path, required=True)
    publication.add_argument("--public-key", type=Path, required=True)
    publication.add_argument("--verified-release", type=Path, required=True)
    publication.add_argument("--repository", type=Path, required=True)
    publication.add_argument("--result", type=Path, required=True)
    live = commands.add_parser("verify-live")
    live.add_argument("--public-key", type=Path, required=True)
    live.add_argument("--verified-release", type=Path, required=True)
    live.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "validate-release":
            validate_release(args.repository, args.tag, args.output)
        elif args.operation == "publish":
            publish(args.package, args.test_report, args.public_key, args.verified_release, args.repository, args.result)
        else:
            verify_live(args.public_key, args.verified_release, args.output)
        return 0
    except (PublicationError, OSError, ValueError) as exc:
        print(f"SmartTar publication stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
