# Changelog

## 0.4.0

Status: Testing. The signed update channel, clean installation, and normal-user migration passed [production verification on November 25, 2026](https://github.com/HaytamElbouhmidi/smarttar/actions/runs/45696716737). Native CachyOS, KDE Plasma, and DEsk testing is still needed.

- Added a package-managed setup route so deliberately published SmartTar releases can be included in normal CachyOS and Arch system updates.
- Added a project-maintained repository with trusted signatures required for both packages and the repository database.
- Added `smarttar --migrate` to move the earlier per-user setup to the packaged command, preserve installed apps and current preferences, and keep the earlier script in a legacy backup.
- Retained `--manual` setup for users who do not want to add the repository. Manual installations do not receive Pacman updates.
- Kept everyday app installation user-level, with no background updater or new startup service. SmartTar's package updates do not update apps installed through SmartTar.
- Verified the public package, database, and manifest signatures, repeated migration, and a normal system update with SmartTar already current. A signed synthetic older-package upgrade passed separately. A production-to-production upgrade awaits a later release.

## 0.3.0

Status: Testing. AppImage installation and menu launching still need verification on the laptop.

- Added installation of `.AppImage` files, including lowercase `.appimage` filenames, alongside `.tar.gz` and `.tgz` archives.
- Added a flow that copies supported AppImages into a permanent user-owned folder, makes only the installed copy executable, and adds an application-menu shortcut without running the download.
- Added lightweight AppImage header and native-processor checks, bounded copying, source-change checks, and rollback on installation failure.
- Added AppImage-specific file associations while preserving earlier archive preferences and later user choices.
- Added AppImage regression tests and plain-language setup, update, and troubleshooting guidance. No background service or additional Python dependencies.

## 0.2.0

Status: Testing. Native CachyOS/KDE Plasma/DEsk and real application archives still need laptop verification.

- Raised archive limits to 8 GiB compressed, 16 GiB extracted payload, 8 GiB per file, and 100,000 entries.
- Added contained relative symbolic links and hardlinks while rejecting escaping, dangling, cyclic, and unsafe link layouts.
- Added recognition for a supported application-bundle layout, selecting its main launcher instead of runtime or helper programs.
- Added a destination free-space check before extraction and retained streaming reads with bounded buffers.
- Added application-bundle layout fixtures, adversarial link tests, and a full-size regression using a valid archive larger than 1 GiB.

## 0.1.2

Status: Testing. Native CachyOS/KDE Plasma/DEsk confirmation is pending.

- Fixed the handler command's compatibility with real `xdg-mime` resolution and corrected desktop argument escaping and working directories.
- Preserved unrelated MIME settings, later user preferences, and rollback information across desktop-session changes.
- Switched inspection and extraction to forward-only streaming with bounded archive and metadata processing.
- Added SHA-256 checks for archive changes, stronger executable detection, and rejection of unsafe archive entries.
- Added read-only `--doctor` diagnostics and explicit failure reporting for unconfirmed setup or failed dialogs.
- Expanded resolver, archive, native-launch, and dialog-flow tests, with a reproducible command-line resource benchmark.

## 0.1.1

- Added KDE and desktop-specific MIME preferences, cache refresh, and association rollback.

## 0.1.0

- Initial archive inspection, portable-app installation, extraction, and KDE file association.
