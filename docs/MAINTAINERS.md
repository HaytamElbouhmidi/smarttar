# Package publishing and signing

SmartTar v0.4.0 | Repository rollout staged for verification

This guide covers the SmartTar update channel. Normal installation and use are described in the [README](../README.md).

## Release model

SmartTar's source stays on `main`. The signed Pacman repository is published on the separate `packages` branch of the same GitHub repository. Package files and repository metadata live under `any/`.

The repository name is `smarttar`. The package server is:

```text
https://raw.githubusercontent.com/HaytamElbouhmidi/smarttar/packages/any
```

Publish only a deliberately selected version tag or manually selected release revision. An ordinary source commit is not an update release. Keep the application version, package version, README, and changelog consistent. Increment the package release when repackaging the same application version requires a new package.

Pacman distributes SmartTar updates during the user's normal system update. There is no SmartTar background service, update timer, or launch-time update request. This channel does not update third-party apps copied into place by SmartTar.

## Signing responsibilities

The public key is stored at `packaging/keys/smarttar.asc`. The pinned full fingerprint is:

```text
89EFC028E6A2EF984615453D47B7FD7E5F99031F
```

The bootstrap must compare the public key's full fingerprint with that pin before granting trust. Keep the pin and exported public key consistent. The public key can be committed. Its private counterpart must never be committed.

The private signing key belongs in the designated GitHub Actions secret, not in source control. Never print it in a workflow log or include it in a package, release asset, screenshot, or diagnostic artifact. Use a private temporary keyring during release jobs and remove it when the job finishes. Retain any necessary recovery material outside the public repository.

Both the package files and the repository database require trusted signatures:

```ini
SigLevel = PackageRequired DatabaseRequired TrustedOnly
```

The public repository must never fall back to optional or disabled signature verification to get around a failed release.

Key replacement is a separate trust change. Document the old and new full fingerprints, update the bootstrap pin, and provide an explicit transition procedure. Do not silently trust a different key downloaded from the package server.

## Before publishing

1. Review the selected source revision and record its immutable commit identifier.
2. Confirm that its declared application and package versions agree with the release notes.
3. Run the application tests and packaging checks for that revision.
4. Build the package in a clean Arch-compatible environment.
5. Confirm the package includes only intended files and declares its runtime requirements.
6. Sign the package and the repository database with the expected release key.
7. Independently verify the exported signatures and expected fingerprint before publishing.
8. Publish the complete repository snapshot, with packages, signatures, and metadata together. Do not replace a release with a different package using the same version and release number.

Repository-write permission and signing-secret access should be limited to the release job. Source validation does not need either. Review the workflow and the selected release revision before granting a job access to the signing secret.

## Build and installed layout

The package recipe is `packaging/arch/PKGBUILD.in`. `tools/build_package.sh` bundles the selected local source, calculates its checksum, and prepares the recipe before invoking `makepkg` as a non-root user. Building a package alone does not sign or publish it.

From a clean, reviewed checkout in an Arch-compatible build environment:

```bash
bash tools/build_package.sh artifacts/package-v0.4.0
```

Use a new output directory. The helper refuses to overwrite its package output. Its `--prepare-only` option prepares the checksummed source and recipe without invoking `makepkg`.

The package owns `/usr/bin/smarttar`, `/usr/lib/smarttar/smarttar.py`, and `/usr/share/applications/smarttar-handler.desktop`, plus its license and short installation guide. The package does not own the user's installed third-party apps, application shortcuts, association rollback record, or legacy-script backups.

`install.sh` separates system setup from desktop migration. Its privileged repository helper installs the signed package, then `/usr/bin/smarttar --migrate` runs as the invoking normal user. Never run desktop migration from a root package hook. It must preserve each user's independent preferences.

## Verify the published repository

Do not mark the v0.4.0 repository setup as ready until the published files, not just local build outputs, pass these checks:

- The public key matches the full fingerprint pinned by bootstrap.
- Pacman accepts the package and repository database with trusted signatures required for both.
- A missing or invalid package signature fails instead of being bypassed.
- A missing or invalid database signature fails instead of being bypassed.
- A clean installation receives SmartTar and its declared dependencies.
- Migration from an earlier per-user installation preserves its existing apps, shortcuts, and association rollback information.
- A later version or package release is found and installed by a normal `sudo pacman -Syu` operation.
- Package removal restores the intended user associations without deleting installed third-party apps.
- Repository removal affects only the SmartTar-managed configuration, not unrelated Pacman settings.
- Existing archive and AppImage installation tests still pass.

Keep the repository's testing status honest. Automated installation in an isolated Arch environment is not the same as successful migration on the CachyOS laptop or graphical validation in KDE Plasma and DEsk.

## If publication fails

Do not announce an update before the signed repository is available and verified. Preserve the last complete working repository snapshot. A transient download or cache mismatch must fail safely and be retried after the publication is consistent, not worked around by weakening signature policy.

Do not republish changed package bytes under an already published version and package release. Issue a new package release or application version as appropriate and record what changed.

If the signing key might be exposed, stop publishing with it and handle key revocation and replacement as an explicit incident. Do not put the private key or recovery material in an issue or commit while investigating.

## Release evidence

Record the source revision, application version, package version and release, public signing fingerprint, package hash, workflow result, and published install or upgrade checks. Keep private keys and credentials out of that evidence.

The existing v0.3.0 screenshots show actual application dialogs. They do not demonstrate Pacman trust setup, repository publication, or an operating-system upgrade, and should not be used as evidence for those checks.
