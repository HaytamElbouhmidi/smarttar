# SmartTar

**Version 0.2.0 | Status: Testing | Linux | CachyOS / Arch | KDE Plasma / DEsk**

I built SmartTar to inspect a downloaded `.tar.gz` or `.tgz` archive from Dolphin and install supported portable applications after confirmation. A compressed archive is not automatically an installer. Source projects and installation scripts get extraction and guidance, not automatic execution.

The archive and integration tests run in isolated Linux environments. Native CachyOS, KDE Plasma, DEsk, and the downloaded Android Studio Rabbit 2 bundle still need laptop verification, so I am keeping this release marked **Testing**.

## Requirements and footprint

- Python 3.10 or later, using only the standard library
- `kdialog` for the native KDE dialogs
- `xdg-utils` for desktop association checks
- A graphical desktop session for interactive use
- `kbuildsycoca6` when available for KDE cache refresh

There are no pip packages, bundled interpreter, browser runtime, daemon, tray process, startup service, or polling loop. SmartTar runs when opened and exits when closed. KDialog uses the system's KDE libraries, so additional package size depends on what is already installed.

Each inspection or extraction streams the archive in one forward pass using bounded buffers. The usual inspect-then-install workflow reads the archive twice, once for each stage, rather than repeatedly seeking through gzip data.

## Installation

1. Download and extract the project, or open an existing checkout.
2. Open a terminal in the folder containing `install.sh`.
3. Install missing system dependencies:

   ```bash
   sudo pacman -S --needed python kdialog xdg-utils
   ```

4. Run the user-level installer without `sudo`:

   ```bash
   bash install.sh
   ```

5. Check the configuration, then double-click `examples/smarttar-smoke-test.tar.gz` in Dolphin:

   ```bash
   python3 ~/.local/share/smarttar/smarttar.py --doctor
   ```

The sample archive contains only a text file. It should open the SmartTar menu and offer extraction, not portable installation. After that check, try a trusted application archive.

An update can be installed over an earlier version. The installer registers `application/x-compressed-tar`, preserves prior preferences, and refreshes the KDE cache when available. It writes only to the current user's directories and respects `XDG_CONFIG_HOME` and `XDG_DATA_HOME`. The commands here use their usual defaults.

Setup exit code **2** means the association was written but activation was **not confirmed**. The installation script stops without printing its success message. Run `--doctor` and check the current desktop session before assuming double-click support works.

## Supported actions

| Archive contents | Action |
| --- | --- |
| Recognized Android Studio Linux bundle | Offer installation using the IDE launcher, preserving its bundled runtime and libraries |
| Executable Linux ELF candidate, including an executable AppImage | Offer user-level installation and create an application launcher |
| `.pkg.tar.gz` with `.PKGINFO` | Show guidance for separately installing a trusted package with pacman |
| `PKGBUILD` | Offer extraction and separate review/build guidance |
| `install.sh`, `setup.sh`, or source project | Offer extraction and guidance without running the contents |
| Other regular files | Offer extraction |

Portable applications go to `~/.local/opt/smarttar/<archive-name>/`. Launchers go to `~/.local/share/applications/` and use the executable's directory as their working directory. Existing installations are not overwritten. Nothing inside the downloaded archive runs during inspection or installation.

**Finding an executable does not prove that an application is portable or compatible.** CPU architecture, runtime libraries, AppImage requirements, and project-specific installation steps remain the publisher's responsibility. Contained relative symbolic links and hardlinks are supported. Unsafe links, sparse files, and special files are rejected. Native `.deb`, `.rpm`, and `.pkg.tar.zst` installation is not supported.

## Android Studio

Version 0.2.0 removes the previous 1 GiB compressed-file barrier. It recognizes Android Studio's `AI` product metadata, matches a Linux launcher to the host architecture, and offers **Install Android Studio** instead of listing Java and internal helper programs. A recognized bundle with invalid metadata or an unsupported launcher is available for extraction and review only.

The supported launcher paths are `bin/studio` and the older `bin/studio.sh`. An older bundle without product metadata can be recognized by its `AI-` build identifier and expected directory layout. Recognition identifies the layout, not the publisher or authenticity of the download.

To test an existing Rabbit 2 download after updating SmartTar:

```bash
python3 ~/.local/share/smarttar/smarttar.py --version
python3 ~/.local/share/smarttar/smarttar.py --inspect ~/Downloads/android-studio-rabbit2-linux.tar.gz
```

The version should be `0.2.0`. A supported current bundle should report `android-studio` and its `android-studio/bin/studio` launcher. Then double-click the archive and confirm installation. SmartTar creates an **Android Studio** application-menu entry without starting the IDE.

Google's [Linux installation guide](https://developer.android.com/studio/install) uses the native `studio` launcher. Android Studio's first-run wizard handles SDK downloads. SmartTar does not install the SDK, configure emulator acceleration, accept licenses, or install system dependencies for the IDE.

The tests use synthetic Android Studio layouts and a real gzip archive larger than 1 GiB. They do not include or redistribute the Android Studio vendor archive. Large archives take time to scan and extract, and this version does not display a progress bar during those stages.

## Archive safeguards

I still verify the publisher and download source before launching any installed program. The SHA-256 check detects archive changes between inspection and extraction. It is not a signature check, malware scan, or proof of publisher trust.

SmartTar refuses absolute member paths, parent traversal in member names, duplicate paths, control characters, sparse files, and special files. Link targets must remain inside the archive and resolve to existing files or directories. Dangling links, link cycles, directory traversal cycles, and archive members beneath link parents are rejected. Links are created only after regular-file extraction and the complete SHA-256 check succeed.

Extracted files do not inherit archive ownership or setuid/setgid bits. SmartTar checks available destination space using payload size plus an overhead allowance before extraction. Quotas and concurrent disk usage can still cause a write to fail. It never executes build scripts, installs dependencies, or elevates privileges.

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

Additional header-count and metadata-nesting limits bound malformed tar processing.

## Diagnostics and extraction

```bash
python3 ~/.local/share/smarttar/smarttar.py --version
python3 ~/.local/share/smarttar/smarttar.py --doctor
python3 ~/.local/share/smarttar/smarttar.py --inspect ~/Downloads/example.tar.gz
python3 ~/.local/share/smarttar/smarttar.py ~/Downloads/example.tar.gz --extract ~/Downloads
```

`--doctor` is read-only. It checks dependencies, session information, the installed handler, and the reported default application. A passing result does not verify that a KDialog window rendered correctly.

If Dolphin still opens Ark, inspect the association and the actual file type:

```bash
xdg-mime query default application/x-compressed-tar
xdg-mime query filetype ~/Downloads/example.tar.gz
```

Expected results are `smarttar-handler.desktop` and `application/x-compressed-tar`. Plain `.gz` files are outside this association. Restart Dolphin after changing settings. If necessary, open KDE File Associations, search for `x-compressed-tar`, and review the preferred application.

## Validation and performance

Run the checks from the project directory:

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh uninstall.sh
python3 tools/benchmark.py --repetitions 3
```

The tests cover malformed and modified archives, contained and adversarial links, Android Studio layouts, installation and rollback, real `xdg-mime` resolution with a competing archive handler, and a locally compiled native fixture that loads a relative asset. The native fixture requires a C compiler and the XDG tests require `xdg-mime`, otherwise those tests are skipped. KDialog responses are mocked for workflow tests. They do not replace a live KDE/DEsk check.

The full-size regression is deliberately skipped during an ordinary test run. Enable it with the following command. It needs approximately 2.1 GiB of temporary disk space and removes its temporary files afterward.

```bash
env SMARTTAR_RUN_LARGE_TESTS=1 python3 -m unittest discover -s tests -v
```

The local v0.2.0 validation run on October 29, 2026 passed all 119 tests with the full-size test enabled and no skips. An ordinary run passes 118 tests and skips the one opt-in full-size test in the same environment. The included GitHub workflow is configured for Python 3.10, 3.12, and 3.14. Its remote results are separate from local test records.

The following v0.2.0 results were measured on Linux 6.18.55 with Python 3.12.14. Times are medians of three runs, and memory is the highest recorded peak process RSS. Filesystem caches were not flushed. KDialog, KDE, and filesystem cache memory are excluded.

| Command-line workload | Time | Peak RSS |
| --- | ---: | ---: |
| Startup with `--version` | 0.131 s | 15.75 MiB |
| Inspect 64 MiB of payload | 0.227 s | 16.54 MiB |
| Inspect and extract 64 MiB of payload | 0.441 s | 20.32 MiB |
| Inspect and extract 5,000 files | 1.509 s | 22.40 MiB |

The separately enabled full-size test inspected and extracted a valid 1,073,832,983-byte gzip archive containing two 512 MiB files. That run measured 1.743 s for inspection, 2.763 s for extraction, and 25.01 MiB peak RSS in a fresh Python process. Fixture generation, KDialog, and KDE are excluded. This is a synthetic archive test, not an Android Studio launch test.

These are measurements from one test environment, not guaranteed timings or total desktop memory requirements. Use the included benchmark to measure the same workloads locally.

## Remove SmartTar

```bash
bash uninstall.sh
```

Removal restores recorded archive preferences while preserving unrelated settings and later user choices. Previously installed portable applications remain available. To remove one of those apps, delete its exact installation folder and matching `smarttar-<archive-name>.desktop` launcher after checking the name.

## Version history and license

See [CHANGELOG.md](CHANGELOG.md) for release changes. MIT license, copyright 2026 Haytam Elbouhmidi, Mechanical Engineer. See [LICENSE](LICENSE).
