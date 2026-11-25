#!/usr/bin/env bash
# SmartTar v0.4.0 checked-source Arch package build
set -euo pipefail

build_helper_version=0.4.0
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"
prepare_only=false

if [[ "${1:-}" == --prepare-only ]]
then
    prepare_only=true
    shift
fi
if [[ $# -ne 1 || "$1" == -* ]]
then
    printf 'Usage: bash tools/build_package.sh [--prepare-only] OUTPUT_DIRECTORY\n' >&2
    exit 2
fi
if [[ "$prepare_only" == false && $(id -u) -eq 0 ]]
then
    printf 'Build SmartTar as a normal user, not root.\n' >&2
    exit 2
fi

for build_tool in python3 tar sha256sum install mktemp
do
    if ! command -v "$build_tool" >/dev/null 2>&1
    then
        printf 'Missing build tool: %s\n' "$build_tool" >&2
        exit 2
    fi
done
if [[ "$prepare_only" == false ]] && ! command -v makepkg >/dev/null 2>&1
then
    printf 'makepkg is required to build an Arch package.\n' >&2
    exit 2
fi

output_dir="$(realpath -m -- "$1")"
if [[ -e "$output_dir" && ( ! -d "$output_dir" || -L "$output_dir" ) ]]
then
    printf 'The output must be a normal directory.\n' >&2
    exit 2
fi
mkdir -p -- "$output_dir"
build_workspace="$(mktemp -d "${TMPDIR:-/tmp}/smarttar-package.XXXXXXXX")"
cleanup_build() {
    if [[ -d "$build_workspace" && ! -L "$build_workspace" ]]
    then
        rm -r -- "$build_workspace"
    fi
}
trap cleanup_build EXIT
mkdir "$build_workspace/source" "$build_workspace/build"

python3 - "$project_dir" "$build_helper_version" <<'PY'
import ast
from pathlib import Path
import sys

root = Path(sys.argv[1])
expected = sys.argv[2]
tree = ast.parse((root / "smarttar.py").read_text(encoding="utf-8"))
versions = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
            and isinstance(node.value, ast.Constant)]
if versions != [expected]:
    raise SystemExit("The application version does not match the package recipe.")
PY

for source_file in smarttar.py LICENSE packaging/arch/smarttar packaging/arch/smarttar-handler.desktop packaging/arch/README.install packaging/arch/PKGBUILD.in
do
    if [[ ! -f "$project_dir/$source_file" || -L "$project_dir/$source_file" ]]
    then
        printf 'Package source is missing or is a symlink: %s\n' "$source_file" >&2
        exit 2
    fi
done
install -m 755 "$project_dir/smarttar.py" "$build_workspace/source/smarttar.py"
install -m 755 "$project_dir/packaging/arch/smarttar" "$build_workspace/source/smarttar"
install -m 644 "$project_dir/LICENSE" "$build_workspace/source/LICENSE"
install -m 644 "$project_dir/packaging/arch/smarttar-handler.desktop" "$build_workspace/source/smarttar-handler.desktop"
install -m 644 "$project_dir/packaging/arch/README.install" "$build_workspace/source/README.install"

release_epoch="${SOURCE_DATE_EPOCH:-1795564800}"
if [[ ! "$release_epoch" =~ ^[0-9]+$ ]]
then
    printf 'SOURCE_DATE_EPOCH must be a nonnegative integer.\n' >&2
    exit 2
fi
source_archive="smarttar-${build_helper_version}.tar"
tar --format=ustar --sort=name --mtime="@$release_epoch" --owner=0 --group=0 --numeric-owner \
    -cf "$build_workspace/build/$source_archive" -C "$build_workspace/source" \
    LICENSE README.install smarttar smarttar-handler.desktop smarttar.py
source_sha256="$(sha256sum "$build_workspace/build/$source_archive")"
source_sha256="${source_sha256%% *}"
python3 - "$project_dir/packaging/arch/PKGBUILD.in" "$build_workspace/build/PKGBUILD" "$source_sha256" <<'PY'
from pathlib import Path
import re
import sys

template, destination, checksum = sys.argv[1:]
text = Path(template).read_text(encoding="utf-8")
if text.count("@SOURCE_SHA256@") != 1 or not re.fullmatch(r"[0-9a-f]{64}", checksum):
    raise SystemExit("Invalid package checksum template.")
Path(destination).write_text(text.replace("@SOURCE_SHA256@", checksum), encoding="utf-8")
PY

if [[ "$prepare_only" == true ]]
then
    for source_file in PKGBUILD "$source_archive"
    do
        if [[ -e "$output_dir/$source_file" || -L "$output_dir/$source_file" ]]
        then
            printf 'Output already exists: %s\n' "$output_dir/$source_file" >&2
            exit 2
        fi
    done
    install -m 644 "$build_workspace/build/PKGBUILD" "$output_dir/PKGBUILD"
    install -m 644 "$build_workspace/build/$source_archive" "$output_dir/$source_archive"
    printf 'Prepared checksummed package sources in %s\n' "$output_dir"
    exit 0
fi

(
    cd "$build_workspace/build"
    SOURCE_DATE_EPOCH="$release_epoch" PKGDEST="$build_workspace/build" makepkg --noconfirm --cleanbuild --clean
)
package_file="smarttar-${build_helper_version}-1-any.pkg.tar.zst"
if [[ ! -f "$build_workspace/build/$package_file" || -L "$build_workspace/build/$package_file" ]]
then
    printf 'makepkg did not produce the expected package: %s\n' "$package_file" >&2
    exit 1
fi
if [[ -e "$output_dir/$package_file" || -L "$output_dir/$package_file" || -e "$output_dir/package-build.json" ]]
then
    printf 'Package outputs already exist. Choose a new output directory.\n' >&2
    exit 2
fi
install -m 644 "$build_workspace/build/$package_file" "$output_dir/$package_file"
python3 - "$output_dir/$package_file" "$source_sha256" "$release_epoch" "$build_helper_version" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

package = Path(sys.argv[1])
metadata = {
    "version": sys.argv[4],
    "package_release": 1,
    "architecture": "any",
    "package": package.name,
    "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
    "source_sha256": sys.argv[2],
    "source_date_epoch": int(sys.argv[3]),
    "signing": "Not signed by this build helper",
}
(package.parent / "package-build.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
PY
printf 'Built %s\n' "$output_dir/$package_file"
