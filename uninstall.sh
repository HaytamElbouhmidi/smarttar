#!/usr/bin/env bash
# SmartTar v0.4.0 per-user association removal
set -euo pipefail

if [[ "$EUID" -eq 0 || -n "${SUDO_USER:-}" || -n "${SUDO_UID:-}" ]]
then
    printf 'Run this script as your normal desktop user, without sudo.\n' >&2
    exit 1
fi

if [[ -f /usr/lib/smarttar/smarttar.py || -e /usr/bin/smarttar || -L /usr/bin/smarttar ]]
then
    if [[ ! -x /usr/bin/smarttar ]]
    then
        printf 'The SmartTar system package is incomplete. Repair it before removing user preferences.\n' >&2
        exit 1
    fi
    /usr/bin/smarttar --restore-association
    printf 'SmartTar preferences were removed for this account. The system package is still installed.\n'
    printf 'To remove the package from this computer, run: sudo pacman -R smarttar\n'
    printf 'Other accounts must restore their own SmartTar preferences before package removal.\n'
    printf 'Previously installed applications and legacy backups have been kept.\n'
    exit 0
fi

data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/smarttar"
script_path="$data_dir/smarttar.py"

if [[ ! -f "$script_path" || -L "$script_path" ]]
then
    printf 'SmartTar installation not found at %s\n' "$script_path" >&2
    exit 1
fi

python3 "$script_path" --restore-association
rm -- "$script_path"
printf 'SmartTar file association and handler removed.\n'
printf 'Previously installed applications, including AppImages, remain in ~/.local/opt/smarttar.\n'
