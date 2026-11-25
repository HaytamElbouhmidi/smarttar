#!/usr/bin/env python3
"""SmartTar v0.4.0 one-time signed Pacman repository setup."""

import argparse
import contextlib
import fcntl
import gzip
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


VERSION = "0.4.0"
FINGERPRINT = "89EFC028E6A2EF984615453D47B7FD7E5F99031F"
SERVER = "https://raw.githubusercontent.com/HaytamElbouhmidi/smarttar/packages/any"
CONFIG = Path("/etc/pacman.conf")
BACKUPS = Path("/var/lib/smarttar/repository-backups")
LOCK = Path("/run/lock/smarttar-repository.lock")
MAX_CONFIG = 2 * 1024 * 1024
MAX_KEY = 64 * 1024
MAX_DATABASE = 4 * 1024 * 1024
BEGIN = "# BEGIN SmartTar managed repository"
END = "# END SmartTar managed repository"
BLOCK = (f"{BEGIN}\n[smarttar]\n"
         "SigLevel = PackageRequired DatabaseRequired TrustedOnly\n"
         f"Server = {SERVER}\n"
         "Usage = Sync Search Install Upgrade\n"
         f"{END}\n").encode("utf-8")
TOOLS = ("pacman", "pacman-key", "pacman-conf", "gpg", "gpgv")


class SetupError(Exception):
    """Setup stopped without weakening package verification."""


def run_tool(name, *arguments, capture=True, timeout=90):
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    if "TERM" in os.environ:
        environment["TERM"] = os.environ["TERM"]
    try:
        result = subprocess.run(
            [f"/usr/bin/{name}", *map(str, arguments)], env=environment,
            capture_output=capture, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError(f"Could not complete {name}. No signature checks were disabled.") from error
    if result.returncode:
        detail = (result.stderr or "").strip()[-2000:] if capture else ""
        raise SetupError(f"{name} did not complete successfully." + (f"\n{detail}" if detail else ""))
    return result.stdout or ""


def read_regular(path, limit, *, owner=None):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > limit:
                raise SetupError(f"Not a supported regular file: {path}")
            if owner is not None and (details.st_uid != owner or details.st_mode & 0o022):
                raise SetupError(f"Unsafe ownership or permissions: {path}")
            content = stream.read(limit + 1)
            if len(content) > limit:
                raise SetupError(f"File is too large: {path}")
            return content, details
    except OSError as error:
        raise SetupError(f"Cannot safely read {path}.") from error


def validate_key_listing(listing, now=None):
    now = int(time.time()) if now is None else now
    records = [line.split(":") for line in listing.splitlines() if line]
    primary = [row for row in records if row[0] == "pub"]
    if len(primary) != 1 or any(row[0] in ("sec", "ssb") for row in records):
        raise SetupError("Use the single SmartTar public signing key, not a private key.")
    row = primary[0]
    if len(row) < 12 or row[1] in ("r", "e", "d", "i") or "s" not in row[11].lower():
        raise SetupError("The SmartTar public signing key is not usable.")
    try:
        if int(row[5]) > now + 300 or (row[6] and int(row[6]) <= now):
            raise SetupError("The signing key is expired or the computer clock is incorrect.")
    except ValueError as error:
        raise SetupError("The public key has invalid validity dates.") from error
    index = records.index(row)
    if index + 1 >= len(records) or records[index + 1][0] != "fpr":
        raise SetupError("The public key fingerprint is missing.")
    fingerprint = records[index + 1]
    if len(fingerprint) < 10 or fingerprint[9] != FINGERPRINT:
        raise SetupError("The public key does not match the pinned SmartTar signing key.")


def prepare_public_key(source, workspace):
    content, _ = read_regular(source, MAX_KEY)
    armor = content.strip()
    if (not armor.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
            or not armor.endswith(b"-----END PGP PUBLIC KEY BLOCK-----")
            or armor.count(b"-----BEGIN PGP") != 1 or b"PRIVATE KEY" in armor):
        raise SetupError("A single armored public key is required.")
    snapshot = workspace / "smarttar.asc"
    snapshot.write_bytes(content)
    listing = run_tool("gpg", "--batch", "--no-options", "--homedir", workspace,
                       "--no-autostart", "--with-colons", "--show-keys", snapshot)
    validate_key_listing(listing)
    keyring = workspace / "public.gpg"
    run_tool("gpg", "--batch", "--no-options", "--homedir", workspace,
             "--no-autostart", "--dearmor", "--output", keyring, snapshot)
    return snapshot, keyring


class RepositoryRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, destination):
        if not allowed_url(destination):
            raise SetupError("The repository redirected to an unexpected address.")
        return super().redirect_request(request, response, code, message, headers, destination)


def allowed_url(url):
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc == "raw.githubusercontent.com"
            and parsed.path.startswith("/HaytamElbouhmidi/smarttar/")
            and not parsed.query and not parsed.fragment)


def download(name, maximum):
    if name not in ("smarttar.db", "smarttar.db.sig"):
        raise SetupError("Unexpected repository metadata name.")
    url = f"{SERVER}/{name}"
    opener = urllib.request.build_opener(RepositoryRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": f"SmartTar/{VERSION}"})
    try:
        with opener.open(request, timeout=30) as response:
            if not allowed_url(response.geturl()) or response.status != 200:
                raise SetupError("The signed repository is not available at its expected address.")
            length = response.headers.get("Content-Length")
            if length and (len(length) > 20 or not length.isdecimal() or int(length) > maximum):
                raise SetupError("The repository metadata is unexpectedly large.")
            content = response.read(maximum + 1)
            if not content or len(content) > maximum:
                raise SetupError("The repository metadata is empty or too large.")
            return content
    except (OSError, urllib.error.URLError) as error:
        raise SetupError("Could not download the signed update information. Check your connection and try again.") from error


def verify_signature_status(status_text):
    records = [line.split() for line in status_text.splitlines() if line.startswith("[GNUPG:] ")]
    forbidden = {"BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG", "NO_PUBKEY", "NODATA",
                 "KEYEXPIRED", "SIGEXPIRED", "KEYREVOKED", "FAILURE", "ERROR"}
    if any(len(row) > 1 and row[1] in forbidden for row in records):
        raise SetupError("The update information has an invalid signature.")
    valid = [row for row in records if len(row) > 2 and row[1] == "VALIDSIG"]
    if (len(valid) != 1 or len(valid[0]) != 12
            or not re.fullmatch(r"[A-F0-9]{40}", valid[0][2])
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", valid[0][3])
            or any(not re.fullmatch(r"[0-9]{1,20}", field) for field in valid[0][4:11])
            or valid[0][-1] != FINGERPRINT):
        raise SetupError("The update information was not signed by the pinned SmartTar key.")


def inspect_database(content):
    descriptors = []
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as compressed:
            expanded = compressed.read(MAX_DATABASE + 1)
        if len(expanded) > MAX_DATABASE:
            raise SetupError("The expanded package database is too large.")
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r|") as database:
            for count, member in enumerate(database, 1):
                path = PurePosixPath(member.name)
                if (count > 64 or path.is_absolute() or ".." in path.parts
                        or not (member.isdir() or member.isfile())):
                    raise SetupError("The signed package database has an unexpected layout.")
                total += member.size
                if member.size < 0 or total > MAX_DATABASE:
                    raise SetupError("The signed package database is too large.")
                if member.isfile():
                    if len(path.parts) != 2 or path.name != "desc" or member.size > 128 * 1024:
                        raise SetupError("The signed package database has unexpected contents.")
                    descriptors.append(database.extractfile(member).read().decode("utf-8"))
    except (tarfile.TarError, OSError, UnicodeError, EOFError) as error:
        raise SetupError("The signed package database could not be read.") from error
    if len(descriptors) != 1:
        raise SetupError("The update channel must contain exactly one SmartTar package record.")
    fields = {}
    for section in descriptors[0].strip().split("\n\n"):
        lines = section.splitlines()
        if len(lines) < 2 or not re.fullmatch(r"%[A-Z0-9]+%", lines[0]) or lines[0] in fields:
            raise SetupError("The signed package record is malformed.")
        fields[lines[0]] = lines[1:]
    version = fields.get("%VERSION%", [""])
    if len(version) != 1 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[1-9][0-9]*", version[0]):
        raise SetupError("The published package version is not supported.")
    filename = f"smarttar-{version[0]}-any.pkg.tar.zst"
    if (fields.get("%NAME%") != ["smarttar"] or fields.get("%ARCH%") != ["any"]
            or fields.get("%FILENAME%") != [filename]
            or not fields.get("%PGPSIG%")
            or not re.fullmatch(r"[a-f0-9]{64}", "".join(fields.get("%SHA256SUM%", [])))):
        raise SetupError("The signed database does not describe the expected SmartTar package.")
    return version[0]


def verify_repository(workspace, keyring):
    database = download("smarttar.db", MAX_DATABASE)
    signature = download("smarttar.db.sig", MAX_KEY)
    database_path = workspace / "smarttar.db"
    signature_path = workspace / "smarttar.db.sig"
    database_path.write_bytes(database)
    signature_path.write_bytes(signature)
    status_text = run_tool("gpgv", "--homedir", workspace, "--keyring", keyring,
                           "--status-fd", "1", signature_path, database_path)
    verify_signature_status(status_text)
    return inspect_database(database)


def configuration_without_block(content):
    try:
        text = content.decode("utf-8")
    except UnicodeError as error:
        raise SetupError("pacman.conf is not valid UTF-8. No settings were changed.") from error
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise SetupError("The SmartTar repository block is incomplete or duplicated.")
    managed = BEGIN in text
    if managed:
        if content.count(BLOCK) != 1:
            raise SetupError("The SmartTar repository block was customized. Review it before continuing.")
        content = content.replace(BLOCK, b"", 1)
        text = content.decode("utf-8")
    if re.search(r"(?m)^\s*\[smarttar\]\s*(?:#.*)?$", text):
        raise SetupError("An unmanaged [smarttar] repository already exists. No settings were changed.")
    return content, managed


def config_with_block(content):
    _, managed = configuration_without_block(content)
    if managed:
        return content
    separator = b"\n" if content.endswith(b"\n") else b"\n\n"
    return content + separator + BLOCK


def check_effective_config(config, managed):
    repositories = run_tool("pacman-conf", "--config", config, "--repo-list").splitlines()
    if repositories.count("smarttar") != int(managed):
        raise SetupError("A conflicting SmartTar repository was found in the Pacman configuration or an included file.")
    if not managed:
        return
    servers = run_tool("pacman-conf", "--config", config, "--repo", "smarttar", "Server").splitlines()
    signatures = run_tool("pacman-conf", "--config", config, "--repo", "smarttar", "SigLevel").split()
    expected = {"PackageRequired", "PackageTrustedOnly", "DatabaseRequired", "DatabaseTrustedOnly"}
    if servers != [SERVER] or set(signatures) != expected:
        raise SetupError("The effective SmartTar server or signature policy differs from the managed settings.")


def secure_directory(path, *, owner=0):
    if not path.is_absolute():
        raise SetupError("Repository setup directories must use absolute paths.")
    if path.parent != path:
        secure_directory(path.parent, owner=owner)
    if not path.exists() and not path.is_symlink():
        try:
            path.mkdir(mode=0o755)
        except FileExistsError:
            pass
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != owner or details.st_mode & 0o022:
        raise SetupError(f"Unsafe repository setup directory: {path}")


def atomic_config_write(path, expected, replacement, details, *, owner=0):
    current, current_details = read_regular(path, MAX_CONFIG, owner=owner)
    if (current != expected or current_details.st_ino != details.st_ino
            or current_details.st_dev != details.st_dev
            or current_details.st_mode != details.st_mode
            or current_details.st_uid != details.st_uid or current_details.st_gid != details.st_gid):
        raise SetupError("Pacman settings changed during setup. No newer settings were overwritten.")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".smarttar-pacman-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(replacement)
            os.fchmod(stream.fileno(), stat.S_IMODE(details.st_mode))
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), details.st_uid, details.st_gid)
            stream.flush()
            os.fsync(stream.fileno())
        latest, latest_details = read_regular(path, MAX_CONFIG, owner=owner)
        if (latest != expected or latest_details.st_ino != details.st_ino
                or latest_details.st_dev != details.st_dev or latest_details.st_mode != details.st_mode
                or latest_details.st_uid != details.st_uid or latest_details.st_gid != details.st_gid):
            raise SetupError("Pacman settings changed during setup. No newer settings were overwritten.")
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def backup_config(content, directory=BACKUPS, *, owner=0):
    secure_directory(directory, owner=owner)
    descriptor, name = tempfile.mkstemp(prefix="pacman.conf.", suffix=".bak", dir=directory)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return Path(name)


@contextlib.contextmanager
def setup_lock(path=LOCK):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != 0 or details.st_mode & 0o022:
            raise SetupError("The repository setup lock has unsafe permissions.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SetupError("Another SmartTar repository setup is already running.") from error
        yield
    finally:
        os.close(descriptor)


def confirm(message):
    try:
        answer = input(message + " [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def install_repository(key_path, config=CONFIG):
    original, details = read_regular(config, MAX_CONFIG, owner=0)
    _, managed = configuration_without_block(original)
    check_effective_config(config, managed)
    with tempfile.TemporaryDirectory(prefix="smarttar-repository-") as directory:
        workspace = Path(directory)
        snapshot, keyring = prepare_public_key(key_path, workspace)
        version = verify_repository(workspace, keyring)
        print(f"Verified SmartTar {version} at {SERVER}")
        print(f"Signing key: {FINGERPRINT}")
        print("This adds a project-maintained update source, not an official Arch or CachyOS repository.")
        print("Pacman will show the full system update and ask before installing it.")
        if not confirm("Trust this SmartTar update source and continue?"):
            raise SetupError("Setup cancelled. No system settings or user preferences were changed.")
        replacement = config_with_block(original)
        backup = backup_config(original) if replacement != original else None
        changed = replacement != original
        try:
            run_tool("pacman-key", "--config", config, "--add", snapshot)
            run_tool("pacman-key", "--config", config, "--lsign-key", FINGERPRINT)
            if changed:
                atomic_config_write(config, original, replacement, details)
            check_effective_config(config, True)
            run_tool("pacman", "--config", config, "-Syu", "--needed", "smarttar/smarttar", capture=False, timeout=None)
            run_tool("pacman", "--config", config, "-Q", "smarttar")
        except (SetupError, OSError, KeyboardInterrupt):
            if changed:
                try:
                    current, current_details = read_regular(config, MAX_CONFIG, owner=0)
                    if current == replacement:
                        atomic_config_write(config, replacement, original, current_details)
                        print("The newly added repository settings were rolled back.", file=sys.stderr)
                    elif current != original:
                        print(f"Pacman settings changed separately. Restore only after reviewing the backup: {backup}", file=sys.stderr)
                except (SetupError, OSError):
                    print(f"Automatic settings recovery could not finish. Review this backup before continuing: {backup}", file=sys.stderr)
            print("Any public signing key imported by this setup remains in Pacman's keyring. No user app files or preferences were changed by this helper.", file=sys.stderr)
            raise
        if backup:
            print(f"Previous Pacman settings saved at {backup}")
    print("The signed SmartTar system package is installed. Desktop migration runs next as your normal user.")


def remove_repository(config=CONFIG):
    original, details = read_regular(config, MAX_CONFIG, owner=0)
    replacement, managed = configuration_without_block(original)
    check_effective_config(config, managed)
    if not managed:
        print("No SmartTar-managed repository settings were found.")
        return
    print("This removes only the SmartTar update source. It does not remove apps or the public signing key.")
    if not confirm("Remove the SmartTar-managed repository settings?"):
        raise SetupError("Repository removal cancelled. No settings were changed.")
    backup = backup_config(original)
    atomic_config_write(config, original, replacement, details)
    print(f"SmartTar update source removed. Previous Pacman settings saved at {backup}")
    print("The SmartTar package and signing key were left in place. This package will no longer receive updates from this source.")


def main():
    parser = argparse.ArgumentParser(description="Set up signed SmartTar system updates.")
    parser.add_argument("--version", action="version", version=f"SmartTar repository setup {VERSION}")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--key", type=Path, help="the bundled SmartTar public key")
    action.add_argument("--remove", action="store_true", help="remove only SmartTar-managed repository settings")
    arguments = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise SetupError("Run bash install.sh as your normal user. It requests permission for system setup.")
        for tool in TOOLS:
            if not os.access(f"/usr/bin/{tool}", os.X_OK):
                raise SetupError(f"{tool} is missing. This setup requires CachyOS or another Arch-compatible system.")
        secure_directory(CONFIG.parent)
        with setup_lock():
            if arguments.remove:
                remove_repository()
            else:
                install_repository(arguments.key)
    except (SetupError, OSError, KeyboardInterrupt) as error:
        print(f"SmartTar: {error or 'Setup cancelled.'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
