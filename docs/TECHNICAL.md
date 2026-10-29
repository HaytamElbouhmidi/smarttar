# Technical details

SmartTar v0.2.0 | Status: Testing

I keep the implementation details, safeguards, diagnostics, and development checks here. For the normal download, installation, and double-click workflow, start with the [README](../README.md).

## Requirements and installed footprint

- Python 3.10 or later, using only the standard library
- `kdialog` for native KDE dialogs
- `xdg-utils` for desktop association checks
- A graphical desktop session for interactive use
- `kbuildsycoca6`, when available, for KDE cache refresh

There are no pip packages, bundled interpreter, browser runtime, daemon, tray process, startup service, or polling loop. SmartTar runs when opened and exits when closed. KDialog uses the system's KDE libraries, so additional dependency size depends on what is already installed.

`install.sh` copies only `smarttar.py` into the current user's application-data directory. It then creates the archive handler, records the prior file-association preferences, updates the association, and refreshes the KDE cache when available. Tests, benchmarks, example archives, screenshots, and documentation are not copied into the installed application or run during normal installation.

The installer writes only to the current user's directories and respects `XDG_CONFIG_HOME` and `XDG_DATA_HOME`. The commands in this document use their usual defaults. Run `bash install.sh` without `sudo`. An update can be installed over an earlier version.

| Item | Default location |
| --- | --- |
| Installed SmartTar code | `~/.local/share/smarttar/smarttar.py` |
| Archive handler | `~/.local/share/applications/smarttar-handler.desktop` |
| Association rollback record | `~/.config/smarttar/association.json` |
| Installed portable applications | `~/.local/opt/smarttar/<archive-name>/` |
| Application launchers | `~/.local/share/applications/smarttar-<archive-name>.desktop` |

## Runtime checks and development tests

The automated test suite is a development tool. The GitHub workflow runs it on GitHub's Ubuntu runners after pushes, pull requests, or a manual workflow start. Those jobs do not run on the laptop when SmartTar opens an archive.

Runtime archive validation is separate. A normal inspect-then-install or inspect-then-extract operation makes two sequential passes through the archive:

1. Inspection reads and decompresses the archive, validates its layout, identifies supported application candidates, and calculates SHA-256 before showing the menu.
2. Extraction reads and decompresses it again while writing files, checking that the contents still match the inspection.

SHA-256 is calculated from bytes already being read. It does not require a separate third pass. Gzip integrity checks happen during those reads. Reviewing the file list or installation guidance reuses the existing inspection instead of rescanning the archive.

The reads use bounded buffers rather than loading the complete payload into memory. The file manifest remains in memory, so usage can grow with file count. Large archives take time to inspect and extract. Version 0.2.0 does not display a progress bar during those stages.

Installed application launchers point directly to the selected executable and use its directory as the working directory. Launching an already installed application does not rerun SmartTar or its archive checks.

## Supported archives and actions

SmartTar handles `.tar.gz` and `.tgz` archives. It registers the `application/x-compressed-tar` file type, not every file ending in `.gz`.

| Archive contents | Action |
| --- | --- |
| Executable Linux ELF candidate, including an executable AppImage | Offer user-level installation and create an application launcher |
| Recognized application bundle with a supported launcher layout | Offer installation using the application launcher, preserving bundled files and libraries |
| `.pkg.tar.gz` with `.PKGINFO` | Show guidance for separately installing a trusted package with pacman |
| `PKGBUILD` | Offer extraction and separate review/build guidance |
| `install.sh`, `setup.sh`, or source project | Offer extraction and guidance without running the contents |
| Other regular files | Offer extraction |

Version 0.2.0 includes a bundle-specific Android Studio recipe using `AI` product metadata or an older `AI-` build identifier and expected layout. Supported launchers are `bin/studio` and the older `bin/studio.sh`. Recognition identifies a supported layout, not the publisher or authenticity of the download. A recognized bundle with invalid metadata or an unsupported launcher is available for extraction and review only.

Finding an executable does not prove that an application is portable or compatible. CPU architecture, runtime libraries, AppImage requirements, and project-specific installation steps still need to match the publisher's requirements. SmartTar does not install system dependencies, compile source code, accept licenses, or automatically run installation scripts. Native `.deb`, `.rpm`, and `.pkg.tar.zst` installation is not supported.

Existing installations are not overwritten. Nothing inside a downloaded archive runs during inspection or installation. Contained relative symbolic links and hardlinks are supported. Unsafe links, sparse files, and special files are rejected.

## Archive safeguards and limits

I still verify the publisher and download source before launching an installed program. The SHA-256 check detects archive changes between inspection and extraction. It is not a signature check, malware scan, or proof of publisher trust.

SmartTar refuses absolute member paths, parent traversal in member names, duplicate paths, control characters, sparse files, and special files. Link targets must remain inside the archive and resolve to existing files or directories. Dangling links, link cycles, directory traversal cycles, and archive members beneath link parents are rejected. Links are created only after regular-file extraction and the complete SHA-256 check succeed.

Extracted files do not inherit archive ownership or setuid/setgid bits. SmartTar checks available destination space using payload size plus an overhead allowance before extraction. Quotas and concurrent disk usage can still cause a write to fail. It never elevates privileges.

| Resource | Limit |
| --- | --- |
| Compressed archive | 8 GiB |
| Total extracted payload | 16 GiB |
| Individual file | 8 GiB |
| Entries / total filesystem nodes including implicit directories | 100,000 / 100,000 |
| One metadata header / total metadata | 1 MiB / 16 MiB |
| Additional decompressed tar overhead | 384 MiB |
| Path length / depth | 4,096 bytes / 128 components |
| Link resolution chain | 40 links |

Additional header-count and metadata-nesting limits bound malformed tar processing. These limits reduce resource abuse. They do not guarantee that every archive below the limits will fit every system's available memory or disk space.

## Diagnostics and command-line extraction

```bash
python3 ~/.local/share/smarttar/smarttar.py --version
python3 ~/.local/share/smarttar/smarttar.py --doctor
python3 ~/.local/share/smarttar/smarttar.py --inspect ~/Downloads/example.tar.gz
python3 ~/.local/share/smarttar/smarttar.py ~/Downloads/example.tar.gz --extract ~/Downloads
```

Replace `example.tar.gz` with the actual archive filename. `--inspect` prints the classification, file count, expanded size, application candidates, and SHA-256 without installing anything. `--extract` creates a new archive-named folder under the selected parent directory.

`--doctor` is read-only. It checks dependencies, session information, the installed handler, and the reported default application. A passing result does not verify that a KDialog window rendered correctly.

Setup exit code **2** means the association was written but activation was **not confirmed**. The installation script stops without printing its success message. Run `--doctor` and check the current desktop session before assuming double-click support works.

If Dolphin still opens Ark, inspect the association and the actual file type:

```bash
xdg-mime query default application/x-compressed-tar
xdg-mime query filetype ~/Downloads/example.tar.gz
```

Expected results are `smarttar-handler.desktop` and `application/x-compressed-tar`. Plain `.gz` files are outside this association. Restart Dolphin after changing settings. If necessary, open KDE File Associations, search for `x-compressed-tar`, and review the preferred application.

The included `examples/smarttar-smoke-test.tar.gz` contains only a text file. After setup, opening it in Dolphin should show the SmartTar menu and offer extraction, not portable installation.

## Development validation

These checks are optional for normal use. Run them from the project directory when developing or verifying a change:

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh uninstall.sh
python3 tools/benchmark.py --repetitions 3
```

The tests cover malformed and modified archives, contained and adversarial links, supported application-bundle layouts, installation and rollback, real `xdg-mime` resolution with a competing archive handler, and a locally compiled native fixture that loads a relative asset. The native fixture requires a C compiler and the XDG tests require `xdg-mime`, otherwise those tests are skipped. KDialog responses are mocked for workflow tests. They do not replace a live KDE/DEsk check.

The full-size regression is deliberately skipped during an ordinary test run. Enable it with the following command. It needs approximately 2.1 GiB of temporary disk space and removes its temporary files afterward.

```bash
env SMARTTAR_RUN_LARGE_TESTS=1 python3 -m unittest discover -s tests -v
```

The local v0.2.0 validation run on October 29, 2026 passed all 119 tests with the full-size test enabled and no skips. An ordinary run passes 118 tests and skips the one opt-in full-size test in the same environment. The GitHub workflow is configured for Python 3.10, 3.12, and 3.14. Its remote results are separate from local test records.

Native CachyOS, KDE Plasma, and DEsk testing with real application archives is still pending. I am keeping this release marked **Testing** until that behavior is verified on the laptop.

## Performance measurements

The following v0.2.0 results were measured on Linux 6.18.55 with Python 3.12.14. Times are medians of three runs, and memory is the highest recorded peak process RSS. Filesystem caches were not flushed. KDialog, KDE, and filesystem cache memory are excluded.

| Command-line workload | Time | Peak RSS |
| --- | ---: | ---: |
| Startup with `--version` | 0.131 s | 15.75 MiB |
| Inspect 64 MiB of payload | 0.227 s | 16.54 MiB |
| Inspect and extract 64 MiB of payload | 0.441 s | 20.32 MiB |
| Inspect and extract 5,000 files | 1.509 s | 22.40 MiB |

These command-line measurements include process startup and shutdown. Inspection and extraction timings include the runtime safeguards. They do not isolate a percentage cost for validation compared with an extractor without those protections.

The separately enabled full-size test inspected and extracted a valid 1,073,832,983-byte gzip archive containing two 512 MiB files. That run measured 1.743 s for inspection, 2.763 s for extraction, and 25.01 MiB peak RSS in a fresh Python process. Fixture generation, KDialog, and KDE are excluded. This fixture uses stored-deflate blocks, so it checks large-file acceptance and bounded-memory behavior rather than representing every application's compression workload.

The full-size test is synthetic, not proof that an arbitrary application will install or launch successfully. These are measurements from one test environment, not guaranteed timings or total desktop memory requirements. Use the included benchmark to measure the same workloads locally.

## Removal and preference restoration

Run the removal script from the project directory without `sudo`:

```bash
bash uninstall.sh
```

Removal restores recorded archive preferences while preserving unrelated settings and later user choices. It removes the SmartTar archive handler and installed program. Previously installed portable applications remain available.

To remove one of those applications, check its exact name, then remove its specific installation folder under `~/.local/opt/smarttar/` and matching `smarttar-<archive-name>.desktop` launcher under `~/.local/share/applications/`. Do not remove the shared application-launcher directory.

## Version history and license

See [CHANGELOG.md](../CHANGELOG.md) for release changes. MIT license, copyright 2026 Haytam Elbouhmidi, Mechanical Engineer. See [LICENSE](../LICENSE).
