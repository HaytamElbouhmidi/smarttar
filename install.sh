#!/usr/bin/env bash
# SmartTar v0.2.0 user-level installation for CachyOS and KDE Plasma
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/smarttar"

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

printf '\nSmartTar v0.2.0 installed for the current user.\n'
printf 'Double-click a .tar.gz archive in Dolphin to inspect it.\n'
printf 'Check this session with: python3 "%s/smarttar.py" --doctor\n' "$data_dir"
