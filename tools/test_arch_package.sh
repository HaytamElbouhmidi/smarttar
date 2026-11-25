#!/usr/bin/env bash
# SmartTar v0.4.0 isolated Arch package and signed-upgrade checks
set -euo pipefail

test_version=0.4.0
if [[ $# -ne 1 || "${1:-}" == -* ]]
then
    printf 'Usage: bash tools/test_arch_package.sh PACKAGE\n' >&2
    exit 2
fi
if [[ $(id -u) -ne 0 || "${SMARTTAR_DISPOSABLE_CONTAINER:-}" != 1 ]]
then
    printf 'Run this test only as root inside a disposable Arch test container.\n' >&2
    exit 2
fi
if [[ ! -f /.dockerenv && ! -f /run/.containerenv ]]
then
    printf 'A disposable container marker was not found.\n' >&2
    exit 2
fi
for test_tool in pacman pacman-key repo-add makepkg gpg bsdtar runuser python3 sha256sum
do
    if ! command -v "$test_tool" >/dev/null 2>&1
    then
        printf 'Missing Arch package test tool: %s\n' "$test_tool" >&2
        exit 2
    fi
done

package_path="$(realpath -e -- "$1")"
if [[ ! -f "$package_path" || -L "$1" ]]
then
    printf 'The package must be a regular file, not a symlink.\n' >&2
    exit 2
fi
build_user="${SMARTTAR_BUILD_USER:-smarttar-build}"
if ! id "$build_user" >/dev/null 2>&1 || [[ $(id -u "$build_user") -eq 0 ]]
then
    printf 'Create a non-root package builder before running this test.\n' >&2
    exit 2
fi
result_path="$(dirname -- "$package_path")/package-test.json"
if [[ -e "$result_path" || -L "$result_path" ]]
then
    printf 'The package test result already exists. Use a fresh output directory.\n' >&2
    exit 2
fi

test_workspace="$(mktemp -d /tmp/smarttar-package-test.XXXXXXXX)"
cleanup_test() {
    if [[ -d "$test_workspace" && ! -L "$test_workspace" ]]
    then
        gpgconf --homedir "$test_workspace/signing" --kill all >/dev/null 2>&1 || true
        gpgconf --homedir "$test_workspace/keyring" --kill all >/dev/null 2>&1 || true
        rm -r -- "$test_workspace"
    fi
}
trap cleanup_test EXIT
chmod 755 "$test_workspace"
root_dir="$test_workspace/root"
repo_dir="$test_workspace/repository"
signing_dir="$test_workspace/signing"
keyring_dir="$test_workspace/keyring"
install -d -m 755 "$root_dir/var/lib/pacman" "$repo_dir" "$test_workspace/cache" "$test_workspace/hooks" "$test_workspace/fixture"
install -d -m 700 "$signing_dir" "$keyring_dir"

python3 - "$package_path" "$test_version" <<'PY'
from pathlib import PurePosixPath
import subprocess
import sys

package, version = sys.argv[1:]
metadata = subprocess.check_output(["bsdtar", "-xOf", package, ".PKGINFO"], text=True)
fields = {}
for line in metadata.splitlines():
    if " = " in line:
        key, value = line.split(" = ", 1)
        fields.setdefault(key, []).append(value)
expected = {
    "pkgname": ["smarttar"],
    "pkgver": [version + "-1"],
    "arch": ["any"],
    "license": ["MIT"],
}
for key, values in expected.items():
    if fields.get(key) != values:
        raise SystemExit(f"Unexpected package metadata for {key}: {fields.get(key)!r}")
if set(fields.get("depend", [])) != {"python>=3.10", "kdialog", "xdg-utils"}:
    raise SystemExit("The runtime dependency list changed unexpectedly.")
allowed = {
    ".PKGINFO", ".BUILDINFO", ".MTREE",
    "usr/bin/smarttar", "usr/lib/smarttar/smarttar.py",
    "usr/share/applications/smarttar-handler.desktop",
    "usr/share/licenses/smarttar/LICENSE", "usr/share/doc/smarttar/README.install",
}
members = subprocess.check_output(["bsdtar", "-tf", package], text=True).splitlines()
actual_files = set()
for member in members:
    name = member.removeprefix("./")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit("Unsafe package member path.")
    if name.endswith("/"):
        if not any(item.startswith(name) for item in allowed):
            raise SystemExit(f"Unexpected package directory: {name}")
    else:
        actual_files.add(name)
if actual_files != allowed:
    raise SystemExit(f"Unexpected package files: {sorted(actual_files ^ allowed)}")
PY

gpg --homedir "$signing_dir" --batch --pinentry-mode loopback --passphrase '' \
    --quick-generate-key 'SmartTar package test key (temporary)' rsa2048 sign 1d
test_fingerprint="$(gpg --homedir "$signing_dir" --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" {print $10; exit}')"
if [[ ! "$test_fingerprint" =~ ^[A-F0-9]{40}$ ]]
then
    printf 'The temporary test signing key has an invalid fingerprint.\n' >&2
    exit 1
fi
gpg --homedir "$signing_dir" --batch --armor --output "$test_workspace/test-key.asc" --export "$test_fingerprint"
pacman-key --gpgdir "$keyring_dir" --init
pacman-key --gpgdir "$keyring_dir" --add "$test_workspace/test-key.asc"
pacman-key --gpgdir "$keyring_dir" --lsign-key "$test_fingerprint"

python3 - "$test_workspace/fixture" <<'PY'
import hashlib
from pathlib import Path
import sys

fixture = Path(sys.argv[1])
source = b'#!/usr/bin/env python3\n# SmartTar v0.3.0 synthetic upgrade fixture\nprint("0.3.0")\n'
(fixture / "smarttar.py").write_bytes(source)
checksum = hashlib.sha256(source).hexdigest()
recipe = '''# SmartTar v0.3.0 synthetic package for upgrade testing
pkgname=smarttar
pkgver=0.3.0
pkgrel=1
pkgdesc='Synthetic SmartTar upgrade fixture'
arch=('any')
license=('MIT')
depends=('python>=3.10' 'kdialog' 'xdg-utils')
options=('!strip' '!debug')
source=('smarttar.py')
sha256sums=('CHECKSUM')
package() {
    install -Dm755 "${srcdir}/smarttar.py" "${pkgdir}/usr/lib/smarttar/smarttar.py"
}
'''
(fixture / "PKGBUILD").write_text(recipe.replace("CHECKSUM", checksum), encoding="utf-8")
PY
chown -R "$build_user:$(id -gn "$build_user")" "$test_workspace/fixture"
(
    cd "$test_workspace/fixture"
    runuser -u "$build_user" -- makepkg --noconfirm --cleanbuild --clean
)
old_package="$test_workspace/fixture/smarttar-0.3.0-1-any.pkg.tar.zst"
new_package="$repo_dir/$(basename -- "$package_path")"
install -m 644 "$package_path" "$new_package"
for signed_package in "$old_package" "$new_package"
do
    gpg --homedir "$signing_dir" --batch --yes --local-user "$test_fingerprint" \
        --output "$signed_package.sig" --detach-sign "$signed_package"
    gpg --homedir "$keyring_dir" --batch --verify "$signed_package.sig" "$signed_package"
done
GNUPGHOME="$signing_dir" repo-add --sign --key "$test_fingerprint" --include-sigs \
    "$repo_dir/smarttar-test.db.tar.gz" "$new_package"
gpg --homedir "$keyring_dir" --batch --verify "$repo_dir/smarttar-test.db.tar.gz.sig" "$repo_dir/smarttar-test.db.tar.gz"
cp --remove-destination "$repo_dir/smarttar-test.db.tar.gz" "$repo_dir/smarttar-test.db"
cp --remove-destination "$repo_dir/smarttar-test.db.tar.gz.sig" "$repo_dir/smarttar-test.db.sig"

python3 - "$test_workspace" "$repo_dir" <<'PY'
from pathlib import Path
import sys

workspace, repository = map(Path, sys.argv[1:])
(workspace / "pacman.conf").write_text(
    "[options]\nArchitecture = auto\nSigLevel = Required TrustedOnly\n"
    "LocalFileSigLevel = Required TrustedOnly\n"
    "[smarttar-test]\nServer = file://" + str(repository) + "\n",
    encoding="utf-8",
)
account = workspace / "root/home/smarttar-test"
app = account / ".local/opt/smarttar/kept-app/payload/keep.txt"
shortcut = account / ".local/share/applications/smarttar-kept-app.desktop"
settings = account / ".config/smarttar/association.json"
for path in (app, shortcut, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
app.write_bytes(b"Existing installed app must remain unchanged.\n")
shortcut.write_bytes(b"[Desktop Entry]\nName=Kept app\nExec=/example/kept-app\n")
settings.write_bytes(b'{"previous_default": "org.kde.ark.desktop"}\n')
PY

pacman_arguments=(
    --root "$root_dir" --dbpath "$root_dir/var/lib/pacman" --cachedir "$test_workspace/cache"
    --gpgdir "$keyring_dir" --logfile "$test_workspace/pacman.log"
    --config "$test_workspace/pacman.conf" --hookdir "$test_workspace/hooks"
    --noconfirm --noscriptlet
    --assume-installed python=3.14 --assume-installed kdialog=99 --assume-installed xdg-utils=99
)
pacman "${pacman_arguments[@]}" -U "$old_package"
test "$(python3 "$root_dir/usr/lib/smarttar/smarttar.py" --version)" = 0.3.0

tampered_package="$test_workspace/tampered.pkg.tar.zst"
install -m 644 "$new_package" "$tampered_package"
install -m 644 "$new_package.sig" "$tampered_package.sig"
printf '\nSMARTTAR TEST TAMPERING\n' >> "$tampered_package"
if gpg --homedir "$keyring_dir" --batch --verify "$tampered_package.sig" "$tampered_package" >/dev/null 2>&1
then
    printf 'A tampered package passed signature verification.\n' >&2
    exit 1
fi
if pacman "${pacman_arguments[@]}" -U "$tampered_package" > "$test_workspace/tampered-package.log" 2>&1
then
    printf 'Pacman accepted a tampered package.\n' >&2
    exit 1
fi
unsigned_package="$test_workspace/unsigned.pkg.tar.zst"
install -m 644 "$new_package" "$unsigned_package"
if pacman "${pacman_arguments[@]}" -U "$unsigned_package" > "$test_workspace/unsigned-package.log" 2>&1
then
    printf 'Pacman accepted an unsigned package.\n' >&2
    exit 1
fi
test "$(python3 "$root_dir/usr/lib/smarttar/smarttar.py" --version)" = 0.3.0

pacman "${pacman_arguments[@]}" -Syu
test "$(python3 "$root_dir/usr/lib/smarttar/smarttar.py" --version)" = "$test_version"
test "$(pacman --root "$root_dir" --dbpath "$root_dir/var/lib/pacman" -Q smarttar)" = "smarttar $test_version-1"

printf '\nSMARTTAR TEST DATABASE TAMPERING\n' >> "$repo_dir/smarttar-test.db"
if gpg --homedir "$keyring_dir" --batch --verify "$repo_dir/smarttar-test.db.sig" "$repo_dir/smarttar-test.db" >/dev/null 2>&1
then
    printf 'A tampered database passed signature verification.\n' >&2
    exit 1
fi
if pacman "${pacman_arguments[@]}" -Syyu > "$test_workspace/tampered-database.log" 2>&1
then
    printf 'Pacman accepted a tampered repository database.\n' >&2
    exit 1
fi
test "$(python3 "$root_dir/usr/lib/smarttar/smarttar.py" --version)" = "$test_version"

python3 - "$test_workspace" "$package_path" "$test_version" "$test_fingerprint" "$result_path" <<'PY'
import hashlib
import json
from pathlib import Path
import stat
import sys

workspace, package = map(Path, sys.argv[1:3])
version, fingerprint, result_path = sys.argv[3:]
account = workspace / "root/home/smarttar-test"
expected = {
    ".local/opt/smarttar/kept-app/payload/keep.txt": b"Existing installed app must remain unchanged.\n",
    ".local/share/applications/smarttar-kept-app.desktop": b"[Desktop Entry]\nName=Kept app\nExec=/example/kept-app\n",
    ".config/smarttar/association.json": b'{"previous_default": "org.kde.ark.desktop"}\n',
}
for relative, contents in expected.items():
    if (account / relative).read_bytes() != contents:
        raise SystemExit(f"The package transaction changed a user file: {relative}")
root = workspace / "root"
for relative in ("usr/bin/smarttar", "usr/lib/smarttar/smarttar.py"):
    if stat.S_IMODE((root / relative).stat().st_mode) != 0o755:
        raise SystemExit(f"Incorrect executable permissions: {relative}")
desktop = (root / "usr/share/applications/smarttar-handler.desktop").read_text(encoding="utf-8")
if "Exec=/usr/bin/smarttar %f\n" not in desktop:
    raise SystemExit("The installed handler does not use the package launcher.")
record = {
    "version": version,
    "package_release": 1,
    "status": "passed",
    "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
    "test_signing_key_fingerprint": fingerprint,
    "production_signing_key_used": False,
    "package_layout_verified": True,
    "package_signature_verified": True,
    "repository_database_signature_verified": True,
    "tampered_package_rejected": True,
    "unsigned_package_rejected": True,
    "tampered_database_rejected": True,
    "upgrade": {"from": "0.3.0-1 synthetic fixture", "to": version + "-1", "command": "pacman -Syu", "isolated_root": True},
    "existing_apps_shortcuts_and_preferences_preserved": True,
    "isolated_root_dependency_assumptions": ["python=3.14", "kdialog=99", "xdg-utils=99"],
    "native_desktop_runtime_validation": False,
}
Path(result_path).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
PY
printf 'SmartTar %s package and signed-upgrade checks passed.\n' "$test_version"
