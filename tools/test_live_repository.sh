#!/usr/bin/env bash
# SmartTar v0.4.0 disposable-container verification of the published update channel
set -euo pipefail

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
if [[ $# -ne 0 || "${SMARTTAR_EXPECTED_VERSION:-}" != 0.4.0 ]]
then
    printf 'This test requires the validated v0.4.0 release and no arguments.\n' >&2
    exit 2
fi
if pacman -Q smarttar >/dev/null 2>&1
then
    printf 'Use a clean container without SmartTar already installed.\n' >&2
    exit 2
fi
build_user="${SMARTTAR_BUILD_USER:-smarttar-build}"
if ! id "$build_user" >/dev/null 2>&1 || [[ $(id -u "$build_user") -eq 0 ]]
then
    printf 'A non-root test account is required.\n' >&2
    exit 2
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
public_key="${SMARTTAR_PUBLIC_KEY:?Provide the checked release public key}"
result_file="${SMARTTAR_LIVE_RESULT:?Provide a new verification result path}"
if [[ ! -f "$public_key" || -L "$public_key" || -e "$result_file" || -L "$result_file" ]]
then
    printf 'Invalid public key or existing test result.\n' >&2
    exit 2
fi

# These confirmations apply only to the clean disposable test container.
printf 'y\ny\n' | /usr/bin/python3 "$script_dir/configure_repository.py" --key "$public_key"
test "$(pacman -Q smarttar)" = "smarttar ${SMARTTAR_EXPECTED_VERSION}-1"
for dependency in python kdialog xdg-utils
do
    pacman -Q "$dependency" >/dev/null
done
pacman -Qk smarttar
test "$(/usr/bin/smarttar --version)" = "$SMARTTAR_EXPECTED_VERSION"

fixture_dir="$(mktemp -d /tmp/smarttar-live-migration.XXXXXXXX)"
chown "$build_user:$(id -gn "$build_user")" "$fixture_dir"
runuser -u "$build_user" -- python3 - "$fixture_dir" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
data = root / "data"
config = root / "config"
legacy = data / "smarttar/smarttar.py"
handler = data / "applications/smarttar-handler.desktop"
settings = config / "smarttar/association.json"
defaults = config / "kde-mimeapps.list"
app = Path.home() / ".local/opt/smarttar/live-test-kept/payload/keep.txt"
shortcut = data / "applications/smarttar-live-test-kept.desktop"
for path in (legacy, handler, settings, defaults, app, shortcut):
    path.parent.mkdir(parents=True, exist_ok=True)
legacy.write_bytes(b"# SmartTar v0.3.0 migration test fixture\n")
handler.write_text(
    '[Desktop Entry]\nType=Application\nName=SmartTar\n'
    f'Exec=python3 "{legacy}" %f\nNoDisplay=true\n', encoding="utf-8",
)
settings.write_bytes(b'{"previous_default": "org.kde.ark.desktop", "overrides": {}}\n')
defaults.write_bytes(
    b"[Default Applications]\napplication/x-compressed-tar=smarttar-handler.desktop;\n"
    b"application/vnd.appimage=another-app.desktop;\n"
)
app.write_bytes(b"An existing app must survive migration and updates.\n")
shortcut.write_bytes(b"[Desktop Entry]\nType=Application\nName=Kept app\nExec=/example/kept-app\n")
manifest = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (settings, defaults, app, shortcut)}
(root / "before.json").write_text(json.dumps(manifest), encoding="utf-8")
PY

for migration_pass in 1 2
do
    runuser -u "$build_user" -- env \
        XDG_DATA_HOME="$fixture_dir/data" XDG_CONFIG_HOME="$fixture_dir/config" \
        XDG_CURRENT_DESKTOP=KDE:DEsk QT_QPA_PLATFORM=offscreen \
        /usr/bin/smarttar --migrate
done

# The first production release has no older production package to upgrade.
pacman -Syu --noconfirm
test "$(pacman -Q smarttar)" = "smarttar ${SMARTTAR_EXPECTED_VERSION}-1"
python3 - "$fixture_dir" "$result_file" "$SMARTTAR_EXPECTED_VERSION" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

fixture, result = map(Path, sys.argv[1:3])
version = sys.argv[3]
before = json.loads((fixture / "before.json").read_text(encoding="utf-8"))
for name, checksum in before.items():
    if hashlib.sha256(Path(name).read_bytes()).hexdigest() != checksum:
        raise SystemExit(f"Existing user data changed: {name}")
legacy = fixture / "data/smarttar/smarttar.py"
if legacy.exists():
    raise SystemExit("The legacy program still shadows package-managed updates.")
backups = list((legacy.parent / "legacy-backups").glob("*/smarttar.py"))
if len(backups) != 1 or backups[0].read_bytes() != b"# SmartTar v0.3.0 migration test fixture\n":
    raise SystemExit("The recoverable legacy backup is missing or changed.")
handler = (fixture / "data/applications/smarttar-handler.desktop").read_text(encoding="utf-8")
if "Exec=/usr/bin/smarttar %f\n" not in handler:
    raise SystemExit("Migration did not select the package-managed command.")
record = {
    "version": version,
    "status": "passed",
    "repository": "https://raw.githubusercontent.com/HaytamElbouhmidi/smarttar/packages/any",
    "production_signing_fingerprint": "89EFC028E6A2EF984615453D47B7FD7E5F99031F",
    "bootstrap_helper_used": True,
    "real_dependency_packages_present": ["python", "kdialog", "xdg-utils"],
    "clean_install_from_public_https_repository": True,
    "signed_database_and_package_required": True,
    "normal_user_migration_and_repeat_migration_passed": True,
    "existing_apps_shortcuts_preferences_and_rollback_record_preserved": True,
    "legacy_program_backed_up": True,
    "ordinary_system_update_command_passed": "pacman -Syu",
    "production_version_upgrade": False,
    "production_upgrade_note": "The first published package is already current after the clean install.",
    "native_cachyos_kde_desk_gui_test": False,
}
result.parent.mkdir(parents=True, exist_ok=True)
with result.open("x", encoding="utf-8") as stream:
    json.dump(record, stream, indent=2)
    stream.write("\n")
PY
printf 'Published SmartTar installation, migration, and normal system update checks passed.\n'
