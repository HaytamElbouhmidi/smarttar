# Technical details

SmartTar v0.4.0 | Status: Testing | Signed update channel verified November 25, 2026

I keep the implementation details, safeguards, diagnostics, and development checks here. For the normal download, installation, and double-click workflow, start with the [README](../README.md).

The public signed update channel passed [production verification on November 25, 2026](https://github.com/HaytamElbouhmidi/smarttar/actions/runs/45696716737), including a clean installation and normal-user migration in a disposable Arch container. Native CachyOS, KDE Plasma, and DEsk testing is still needed. The [release verification record](releases/v0.4.0-verification.json) records the exact source, package, signatures, and limits of those checks.

## Requirements and installed footprint

- Python 3.10 or later, using only the standard library
- `kdialog` for native KDE dialogs
- `xdg-utils` for desktop association checks
- A graphical desktop session for interactive use
- `kbuildsycoca6`, when available, for KDE cache refresh

The one-time package bootstrap also needs system Python, `sudo`, and an Arch-compatible Pacman installation. The packaged runtime declares Python, KDialog, and xdg-utils as dependencies. The package does not bundle their runtimes.

There are no pip packages, bundled interpreter, browser runtime, daemon, tray process, startup service, or polling loop. SmartTar runs when opened and exits when closed. KDialog uses the system's KDE libraries, so additional dependency size depends on what is already installed.

The published `smarttar-0.4.0-1-any.pkg.tar.zst` is 24,609 bytes, about 24 KiB. Pacman reports a rounded installed size of 0.07 MiB. These figures cover SmartTar's own package, not system dependencies, user data, or apps installed through SmartTar.

The default `bash install.sh` route adds the SmartTar project's signed Pacman repository and installs the project package. System-level package and repository changes require administrator permission. Desktop preference setup and migration run afterward as the invoking normal user.

`bash install.sh --manual` retains the earlier per-user installation. It copies `smarttar.py` into the current user's application-data directory and configures the download handler without adding a package repository. Manual installations need to be updated by rerunning manual setup from a newer source download.

Both modes record prior file-association preferences, refresh the KDE cache when available, and respect `XDG_CONFIG_HOME` and `XDG_DATA_HOME` for per-user data. The commands in this document use their usual defaults. Start the installer without `sudo` so desktop setup is not applied to root's account. No development test or screenshot workflow runs during setup or everyday use.

| Item | Default location |
| --- | --- |
| Packaged command | `/usr/bin/smarttar` |
| Packaged SmartTar code | `/usr/lib/smarttar/smarttar.py` |
| Packaged download handler | `/usr/share/applications/smarttar-handler.desktop` |
| Manual SmartTar code | `~/.local/share/smarttar/smarttar.py` |
| Per-user download handler | `~/.local/share/applications/smarttar-handler.desktop` |
| Association rollback record | `~/.config/smarttar/association.json` |
| Earlier program saved during migration | `~/.local/share/smarttar/legacy-backups/v0.4.0-<unique-suffix>/smarttar.py` |
| Installed applications | `~/.local/opt/smarttar/<download-name>/` |
| Installed AppImage copy | `~/.local/opt/smarttar/<download-name>/payload/<original-filename>` |
| Application launchers | `~/.local/share/applications/smarttar-<download-name>.desktop` |

`<download-name>` is a normalized name derived from the downloaded filename, without `.tar.gz`, `.tgz`, or `.AppImage`. AppImage suffix matching is case-insensitive, and its original filename is retained inside `payload`.

`pacman -Ql smarttar` lists the full system package contents, including its license and short installation guide. The user-installed applications and their launchers are separate from those package-owned files.

## Package-managed updates and migration

The update repository is named `smarttar` and is maintained by this project. It is not an official Arch Linux or CachyOS repository. Its package files and signed database are published to the `packages` branch of this repository, under `any/`:

```text
https://raw.githubusercontent.com/HaytamElbouhmidi/smarttar/packages/any
```

The public signing key is kept in `packaging/keys/smarttar.asc`. Its pinned full fingerprint is:

```text
89EFC028E6A2EF984615453D47B7FD7E5F99031F
```

Bootstrap checks the exported public key against that fingerprint before setting up trust. The repository's signature policy is:

```ini
SigLevel = PackageRequired DatabaseRequired TrustedOnly
```

A missing, untrusted, or invalid package or database signature is an installation or update failure, not a reason to disable checks. This authenticates the project package against the trusted project key. The first installation still depends on obtaining the installer from a source you trust. It does not authenticate unrelated apps later downloaded and installed through SmartTar.

The default installer invokes `tools/configure_repository.py --key packaging/keys/smarttar.asc` with administrator permission, then runs `/usr/bin/smarttar --migrate` as the original desktop user. The repository helper requests this interactive package operation:

```bash
pacman -Syu --needed smarttar/smarttar
```

That operation updates the system as well as installing the package explicitly from the SmartTar repository. Review Pacman's proposed changes. This is not a partial `-Sy` package update.

After successful one-time setup, normal full-system updates include deliberately published SmartTar releases:

```bash
sudo pacman -Syu
```

There is no separate background updater, timer, startup task, or per-launch update query. A commit pushed to `main` does not by itself become a package update. Package publication is a deliberate version-tag or manual-release operation. See [maintainer operations](MAINTAINERS.md).

The update source changes how **SmartTar itself** is installed and updated. It does not convert AppImages or extracted portable apps into Pacman packages. Those applications remain under `~/.local/opt/smarttar/` and need their publisher's updater or a newer download.

### Migrating an earlier installation

Run the new default installer once as the normal desktop user. The migration switches the SmartTar handler from the earlier user-local script to the package-managed program. It preserves the rollback record, current file-opening preferences, installed applications, and their menu entries. A new account without an earlier setup receives the normal SmartTar association setup.

The earlier user-local script is moved into a uniquely named folder under `~/.local/share/smarttar/legacy-backups/`. It is not deleted. The per-user handler points to `/usr/bin/smarttar`, so it follows package upgrades instead of starting the saved older script.

If the system package is installed but migration was interrupted, retry as the normal user:

```bash
smarttar --migrate
smarttar --doctor
```

Migration refuses an unrecognized customized handler instead of overwriting it. Existing choices of another default application are preserved. If you intentionally want to select SmartTar as the default again, run `smarttar --setup` separately.

Migration must not run under root's desktop profile, delete `~/.local/opt/smarttar/`, or remove the shared application-launcher directory. A failure should be reported explicitly. Check the installed package and current desktop configuration before treating the move as complete.

Do not keep a manual SmartTar copy active alongside the packaged handler. A user-local desktop entry can otherwise take precedence over a system entry and continue starting an old copy. The manual installer refuses to proceed when the system package's command or program is present.

## Runtime checks and development tests

The automated test suite is a development tool. The GitHub workflow runs it on GitHub's Ubuntu runners after pushes, pull requests, or a manual workflow start. Those jobs do not run on the laptop when SmartTar opens an archive.

Runtime archive validation is separate. A normal inspect-then-install or inspect-then-extract operation makes two sequential passes through the archive:

1. Inspection reads and decompresses the archive, validates its layout, identifies supported application candidates, and calculates SHA-256 before showing the menu.
2. Extraction reads and decompresses it again while writing files, checking that the contents still match the inspection.

SHA-256 is calculated from bytes already being read. It does not require a separate third pass. Gzip integrity checks happen during those reads. Reviewing the file list or installation guidance reuses the existing inspection instead of rescanning the archive.

The reads use bounded buffers rather than loading the complete payload into memory. The file manifest remains in memory, so usage can grow with file count. Large archives take time to inspect and extract. Version 0.4.0 does not display a progress bar during those stages.

AppImages follow a separate path. Inspection reads at most 64 header bytes and checks the file's metadata. Installation streams one copy into a private staging directory, calculating SHA-256 from the bytes already being copied. It checks that the source has not changed before committing the installation. It does not scan the complete file before showing the menu, decompress the AppImage, or run the AppImage runtime to obtain its metadata or icon.

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

The bundle-specific Android Studio recipe introduced in v0.2.0 uses `AI` product metadata or an older `AI-` build identifier and expected layout. Supported launchers are `bin/studio` and the older `bin/studio.sh`. Recognition identifies a supported layout, not the publisher or authenticity of the download. A recognized bundle with invalid metadata or an unsupported launcher is available for extraction and review only.

Finding an executable does not prove that an application is portable or compatible. CPU architecture, runtime libraries, AppImage requirements, and project-specific installation steps still need to match the publisher's requirements. SmartTar does not install system dependencies, compile source code, accept licenses, or automatically run installation scripts. Native `.deb`, `.rpm`, and `.pkg.tar.zst` installation is not supported.

Existing installations are not overwritten. Nothing inside a downloaded archive runs during inspection or installation. Contained relative symbolic links and hardlinks are supported. Unsafe links, sparse files, and special files are rejected.

## AppImage support

Since v0.3.0, SmartTar also accepts standalone `.AppImage` files, with any capitalization of the suffix. These are copied as one file rather than passed to the archive extractor.

Recognition checks the ELF executable header and the AppImage marker at byte offset 8. Marked type 1 (`AI` followed by byte `1`) and type 2 (`AI` followed by byte `2`) files are supported. Some historical type 1 AppImages do not include that marker and are not accepted. An ordinary executable renamed to `.AppImage` is not sufficient.

The executable's processor architecture must match the supported native architecture of the current computer. This check avoids installing an ARM download on an x86-64 laptop, for example. It does not prove that every instruction, graphics driver, or system library required by the program is available.

Installation:

1. Checks the source is a regular file and fits the 8 GiB limit.
2. Checks destination space and refuses to replace an existing installation or shortcut.
3. Copies the file with bounded buffers into a private staging directory and checks for source changes.
4. Sets the installed copy's permissions to `0755`, without changing the original download.
5. Records installation metadata and publishes a shortcut that launches the installed copy directly.

The AppImage is not executed during inspection or installation and is not launched automatically afterward. The installed shortcut uses a generic application icon. SmartTar does not run `--appimage-extract`, `--appimage-mount`, or another command inside the downloaded file to obtain an icon or desktop metadata.

These checks recognize a supported file layout, not a trusted publisher. SHA-256 recorded while copying is not a signature verification or malware scan. The app can run code with your user account's access when you later launch it.

### AppImage file associations

SmartTar registers these AppImage-specific file types in addition to `application/x-compressed-tar`:

- `application/vnd.appimage`
- `application/x-iso9660-appimage`
- `application/x-appimage`

It does not take over the generic Linux executable or unknown-file type. Existing v0.2.0 archive-association rollback records are retained when updating. AppImage preferences are recorded separately so uninstall can restore them without replacing unrelated settings or later explicit choices.

File associations do not override every desktop's executable-file policy. An AppImage that already has executable permission may run directly when double-clicked. Right-click the download and select **Open With SmartTar** to use the installer. SmartTar does not install a privileged execution hook or background interception service.

### AppImage troubleshooting

If SmartTar is not offered in **Open With**, rerun `bash install.sh` from the latest extracted project folder and restart Dolphin. Check the detected type and registered handler:

```bash
xdg-mime query filetype ~/Downloads/example.AppImage
xdg-mime query default application/vnd.appimage
xdg-mime query default application/x-iso9660-appimage
xdg-mime query default application/x-appimage
```

Replace `example.AppImage` with the actual filename. The default-handler queries should report `smarttar-handler.desktop` after successful setup. If your system reports a generic executable type, select SmartTar explicitly through **Open With**. Do not associate every Linux executable with SmartTar to force this behavior.

If the menu shortcut is created but the application does not start, check the publisher's requirements. AppImages still depend on compatible system facilities. Some older AppImages need FUSE 2 libraries, while newer runtimes may not. SmartTar does not require or automatically install `fuse2` for every AppImage and does not disable application sandboxes to work around a startup error.

For an unrecognized header or processor mismatch, check that the download is complete and that you selected the correct Linux build for your computer. Redownloading from the publisher is preferable to renaming an unrelated file or bypassing the check.

## Archive safeguards and limits

I still verify the publisher and download source before launching an installed program. The SHA-256 check detects archive changes between inspection and extraction. It is not a signature check, malware scan, or proof of publisher trust.

SmartTar refuses absolute member paths, parent traversal in member names, duplicate paths, control characters, sparse files, and special files. Link targets must remain inside the archive and resolve to existing files or directories. Dangling links, link cycles, directory traversal cycles, and archive members beneath link parents are rejected. Links are created only after regular-file extraction and the complete SHA-256 check succeed.

Extracted files do not inherit archive ownership or setuid/setgid bits. SmartTar checks available destination space using payload size plus an overhead allowance before extraction. Quotas and concurrent disk usage can still cause a write to fail. Installing a downloaded archive or AppImage never elevates privileges. Administrator permission is limited to installing or updating the SmartTar system package and configuring its repository.

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

For the package-managed installation:

```bash
smarttar --version
smarttar --doctor
smarttar --inspect ~/Downloads/example.tar.gz
smarttar --inspect ~/Downloads/example.AppImage
smarttar ~/Downloads/example.tar.gz --extract ~/Downloads
```

For a manual installation, replace `smarttar` at the beginning of each command with `python3 ~/.local/share/smarttar/smarttar.py`. Do not use a legacy backup to diagnose the current package.

Replace `example.tar.gz` with the actual archive filename. `--inspect` prints the classification, file count, expanded size, application candidates, and SHA-256 without installing anything. `--extract` creates a new archive-named folder under the selected parent directory.

For an AppImage, `--inspect` prints the detected type, processor architecture, and file size without executing or installing it. It does not read the entire file to calculate a pre-installation hash. `--extract` is only for archives and refuses AppImages.

`--doctor` is read-only. It reports the installation mode and checks dependencies, session information, the installed handler, and the reported default applications. A passing result does not verify that a KDialog window rendered correctly, that a future package update exists, or that an AppImage's own runtime works.

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

AppImage tests additionally cover header recognition, case-insensitive suffixes, processor checks, bounded copying, unchanged originals, safe shortcut creation, no-overwrite behavior, source changes, failure cleanup, and multi-type preference restoration. Synthetic AppImage fixtures are used for these checks. They are not evidence that a particular publisher's real AppImage has launched successfully.

The full-size regression is deliberately skipped during an ordinary test run. Enable it with the following command. It needs approximately 2.1 GiB of temporary disk space and removes its temporary files afterward.

```bash
env SMARTTAR_RUN_LARGE_TESTS=1 python3 -m unittest discover -s tests -v
```

The local v0.2.0 validation run on October 29, 2026 passed all 119 tests with the full-size test enabled and no skips. An ordinary run passes 118 tests and skips the one opt-in full-size test in the same environment. The GitHub workflow is configured for Python 3.10, 3.12, and 3.14. Its remote results are separate from local test records.

The v0.2.0 counts above are historical, not a count of the current test suite. Use the latest GitHub workflow result for the current revision.

On November 25, 2026, the local v0.3.0 suite passed all 163 tests on Python 3.12 with the full-size regression enabled and no skips. The 44 added AppImage checks include isolated real `xdg-mime` resolution and a locally compiled synthetic marked runtime. The latter is deliberately started only after installation to check its executable copy and working directory. It is not a publisher AppImage compatibility test.

The v0.4.0 source suite contains 306 tests, with skips depending on the environment and whether the large-file test is enabled. The Python 3.10, 3.12, and 3.14 GitHub jobs passed on November 25, 2026, including the archive larger than 1 GiB on Python 3.12. Arch package validation also passed, including 69 repository-bootstrap checks and validation against real `pacman-conf` output.

The [successful production workflow](https://github.com/HaytamElbouhmidi/smarttar/actions/runs/45696716737) separately verified the published update channel:

- Downloaded the public HTTPS package, database, and release manifest, then checked their signatures.
- Installed `smarttar 0.4.0-1` through the real bootstrap with Python, KDialog, and xdg-utils present. `pacman -Qk` reported 15 files and no missing files.
- Ran normal-user migration twice. Existing payloads, shortcuts, file-opening preferences, and the rollback record were preserved, with one recoverable backup of the earlier SmartTar script.
- Completed a normal `pacman -Syu` with SmartTar already current. This is a no-op for SmartTar, not a production-to-production version-upgrade test.

A separate isolated Pacman test upgraded a signed synthetic `0.3.0` package to `0.4.0` and rejected tampered packages, unsigned packages, and a tampered database. That test uses a disposable signing key. A later production release is needed to verify an upgrade between two production package versions. See the [release record](releases/v0.4.0-verification.json) for the exact evidence and scope.

Native CachyOS, KDE Plasma, and DEsk testing with real application archives and AppImages is still pending. I am keeping this release marked **Testing** until that behavior is verified on the laptop.

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

### Package-managed installation

Restore this account's recorded archive and AppImage preferences before removing the system package:

```bash
smarttar --restore-association
sudo pacman -R smarttar
```

Run the first command as the normal desktop user, without `sudo`. On a shared computer, every account that configured SmartTar should restore its own preferences before an administrator removes the package. The first command needs the installed SmartTar program, so do it before the Pacman command.

Running `bash uninstall.sh` while the system package is present performs only the current account's preference restoration and prints the package-removal instructions. It does not remove the system package itself.

To stop using the update source as well, use the latest extracted project folder:

```bash
sudo python3 tools/configure_repository.py --remove
```

This removes only the SmartTar-managed repository configuration, not unrelated Pacman settings. It leaves the signing key and its existing trust in place. Repository removal is separate from package removal and key revocation. Do not remove a shared signing key as a shortcut for removing a repository entry.

### Manual installation

Run the removal script from the project directory as the normal desktop user:

```bash
bash uninstall.sh
```

When no system package is present, it restores the recorded preferences and removes the active user-local SmartTar script. Unrelated settings, later explicit file-opening choices, and legacy backups are kept.

### Applications installed through SmartTar

Previously installed portable applications and AppImages remain available after either removal method.

To remove one of those applications, check its exact name, then remove its specific installation folder under `~/.local/opt/smarttar/` and matching `smarttar-<download-name>.desktop` launcher under `~/.local/share/applications/`. Do not remove the shared application-launcher directory. Removing SmartTar or an installed copy does not remove the original download.

## Version history and license

See [CHANGELOG.md](../CHANGELOG.md) for release changes. MIT license, copyright 2026 Haytam Elbouhmidi, Mechanical Engineer. See [LICENSE](../LICENSE).
