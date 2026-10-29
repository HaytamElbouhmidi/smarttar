#!/usr/bin/env bash
# SmartTar v0.2.0 association removal
set -euo pipefail

data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/smarttar"
script_path="$data_dir/smarttar.py"

if [[ ! -f "$script_path" ]]
then
    printf 'SmartTar installation not found at %s\n' "$script_path" >&2
    exit 1
fi

python3 "$script_path" --restore-association
rm -- "$script_path"
printf 'SmartTar file association and handler removed.\n'
printf 'Previously installed portable applications remain in ~/.local/opt/smarttar.\n'
