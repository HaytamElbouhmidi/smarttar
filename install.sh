#!/usr/bin/env bash
# SmartTar v0.4.0 package setup and optional manual installation
set -euo pipefail

if [[ "$EUID" -eq 0 || -n "${SUDO_USER:-}" || -n "${SUDO_UID:-}" ]]
then
    printf 'Run this installer as your normal desktop user, without sudo.\n' >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/smarttar"
system_program='/usr/lib/smarttar/smarttar.py'
system_command='/usr/bin/smarttar'

if [[ "$#" -gt 1 || ( "$#" -eq 1 && "$1" != '--manual' ) ]]
then
    printf 'Usage: bash install.sh [--manual]\n' >&2
    exit 1
fi

if [[ "$#" -eq 0 ]]
then
    helper="$script_dir/tools/configure_repository.py"
    public_key="$script_dir/packaging/keys/smarttar.asc"
    if [[ ! -f "$helper" || ! -f "$public_key" ]]
    then
        printf 'The signed repository setup files are missing. Download and extract the complete SmartTar package.\n' >&2
        exit 1
    fi
    if [[ ! -x /usr/bin/python3 ]] || ! command -v sudo >/dev/null 2>&1
    then
        printf 'Python and sudo are required for the one-time system package setup.\n' >&2
        exit 1
    fi
    printf 'Setting up signed SmartTar updates. The system setup will ask for your password.\n'
    sudo /usr/bin/python3 "$helper" --key "$public_key"
    if [[ ! -f "$system_program" || ! -x "$system_command" ]]
    then
        printf 'The SmartTar system package is not available. Existing user settings were not changed.\n' >&2
        exit 1
    fi
    "$system_command" --migrate
    printf '\nSmartTar is ready. Update it with your normal system updates.\n'
    exit 0
fi

if [[ -e "$system_program" || -L "$system_program" || -e "$system_command" || -L "$system_command" ]]
then
    printf 'SmartTar already has a system package. Manual installation would hide its updates.\n' >&2
    printf 'Run bash install.sh without --manual to finish package setup.\n' >&2
    exit 1
fi

for required in python3 kdialog xdg-mime
do
    if ! command -v "$required" >/dev/null 2>&1
    then
        printf 'Missing dependency: %s\n' "$required" >&2
        printf 'Install it with: sudo pacman -S python kdialog xdg-utils\n' >&2
        exit 1
    fi
done

mkdir -p "$data_dir"
install -m 755 "$script_dir/smarttar.py" "$data_dir/smarttar.py"
python3 "$data_dir/smarttar.py" --setup

printf '\nSmartTar v0.4.0 installed manually for the current user.\n'
printf 'This manual installation does not receive package-manager updates.\n'
printf 'Open .tar.gz, .tgz, or .AppImage downloads with SmartTar in Dolphin.\n'
printf 'For AppImages, use right-click > Open With > SmartTar if double-click runs the app directly.\n'
printf 'Check this session with: python3 "%s/smarttar.py" --doctor\n' "$data_dir"
