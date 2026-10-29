# Changelog

## 0.2.0

Status: Testing. Native CachyOS/KDE Plasma/DEsk and the downloaded Android Studio bundle still need laptop verification.

- Raised archive limits to 8 GiB compressed, 16 GiB extracted payload, 8 GiB per file, and 100,000 entries.
- Added contained relative symbolic links and hardlinks while rejecting escaping, dangling, cyclic, and unsafe link layouts.
- Added Android Studio bundle recognition that selects the IDE launcher instead of bundled Java or helper programs.
- Added a destination free-space check before extraction and retained streaming reads with bounded buffers.
- Added Android Studio layout fixtures, adversarial link tests, and a full-size regression using a valid archive larger than 1 GiB.

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
