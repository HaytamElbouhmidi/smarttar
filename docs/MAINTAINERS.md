# Package publishing and signing

SmartTar v0.4.0 | Repository rollout staged for verification

This guide covers the SmartTar update channel. Normal installation and use are described in the [README](../README.md).

## Release model

SmartTar's source stays on `main`. The signed Pacman repository is published on the separate `packages` branch of the same GitHub repository. Package files and repository metadata live under `any/`.

The repository name is `smarttar`. The package server is:

```text
https://raw.githubusercontent.com/HaytamElbouhmidi/smarttar/packages/any
```

Publish only a deliberately selected stable version tag. The workflow can start when that tag is pushed or be started manually for the same existing tag. An ordinary commit to `main` is not published as a package update. Keep the application version, package version, README, and changelog consistent. Increment the package release when repackaging the same application version requires a new package.

The `packages` branch is artifact-only. It contains the signed package, repository database, release manifest, and accompanying signatures, not a working copy of the application source. Published package versions are not overwritten, and older package files are retained for clients using a cached database. An already-current rerun verifies the existing signed release instead of replacing it.

Pacman distributes SmartTar updates during the user's normal system update. There is no SmartTar background service, update timer, or launch-time update request. This channel does not update third-party apps copied into place by SmartTar.

## Signing responsibilities

The public key is stored at `packaging/keys/smarttar.asc`. The pinned full fingerprint is:

```text
89EFC028E6A2EF984615453D47B7FD7E5F99031F
```

The bootstrap must compare the public key's full fingerprint with that pin before granting trust. Keep the pin and exported public key consistent. The public key can be committed. Its private counterpart must never be committed.

The private signing key is supplied through the repository's GitHub Actions secret `SMARTTAR_SIGNING_KEY`, which is already configured. It is not source code and must never be committed. Never print it in a workflow log or include it in a package, release asset, screenshot, or diagnostic artifact. The release job uses a private temporary keyring and removes it afterward. Retain any necessary recovery material outside the public repository.

Both the package files and the repository database require trusted signatures:

```ini
SigLevel = PackageRequired DatabaseRequired TrustedOnly
```

The public repository must never fall back to optional or disabled signature verification to get around a failed release.

Key replacement is a separate trust change. Document the old and new full fingerprints, update the bootstrap pin, and provide an explicit transition procedure. Do not silently trust a different key downloaded from the package server.

## Publish a release

The workflow is **Publish SmartTar packages**, defined in [publish-packages.yml](../.github/workflows/publish-packages.yml).

1. Finish reviewing the release, merge its changes into `main`, and check that application and package validation have passed for the intended source commit.
2. Confirm that the application version, package recipe, and changelog agree. For this release, the stable tag is `v0.4.0`.
3. Create and push that tag so it resolves to the reviewed commit on `main`. Record the full commit identifier. Do not move an existing published release tag to different code.
4. Open the repository's **Actions** tab and select **Publish SmartTar packages**. Pushing the tag starts it automatically. To start or repeat the release manually, select **Run workflow**, choose branch **main**, and enter `v0.4.0` in **release_tag**. The tag must already exist.
5. Wait for the whole workflow to pass, including the checks of the published public repository. A completed build or signing step alone does not prove the update channel works.
6. Download the **smarttar-release-verification** artifact from the successful run and keep its release evidence. Only then announce that the package update is available.

The workflow validates the stable tag, matching version, and trusted `main` ancestry before running the selected release code. It runs the application regression suite, builds as a non-root user, and tests a signed package upgrade with a disposable test key before the production signing key is made available.

`SMARTTAR_SIGNING_KEY` is passed only to the signing step. The tested package, database, and manifest are signed and checked before one non-force update publishes the complete artifact snapshot. Ordinary `main` pushes do not trigger this publication workflow.

For a rerun of an already-current release, leave the tag and package version unchanged. The publisher rechecks the retained signed files and reports that the release is already current. If the package contents need to change, publish a new package release or application version instead of overwriting the existing one.

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
- A real Pacman upgrade from the older synthetic package to the new package passes separately from the production-repository checks.
- Package removal restores the intended user associations without deleting installed third-party apps.
- Repository removal affects only the SmartTar-managed configuration, not unrelated Pacman settings.
- Existing archive and AppImage installation tests still pass.

Keep the repository's testing status honest. Automated installation in an isolated Arch environment is not the same as successful migration on the CachyOS laptop or graphical validation in KDE Plasma and DEsk.

The workflow checks actual public HTTPS downloads after publishing, including the package, repository database, manifest, and their signatures. `tools/test_live_repository.sh` then uses the real bootstrap helper in a disposable Arch container, installs from that public repository, and verifies normal-user migration and preservation of existing apps and preferences.

For the first production release, the subsequent `pacman -Syu` check is deliberately a no-op for SmartTar because the clean installation is already current. It is not evidence of a production-version upgrade. The actual older-version transition is tested separately using a synthetic `0.3.0` package and the `0.4.0` package with a disposable signing key. A future production release is needed to verify a real production-to-production upgrade.

## If publication fails

Do not announce an update before the signed repository is available and verified. Preserve the last complete working repository snapshot. A transient download or cache mismatch must fail safely and be retried after the publication is consistent, not worked around by weakening signature policy.

Do not republish changed package bytes under an already published version and package release. Issue a new package release or application version as appropriate and record what changed.

If the signing key might be exposed, stop publishing with it and handle key revocation and replacement as an explicit incident. Do not put the private key or recovery material in an issue or commit while investigating.

## Release evidence

Record the source revision, application version, package version and release, public signing fingerprint, package hash, workflow result, and published install or upgrade checks. The **smarttar-release-verification** artifact contains the validated release, build, signed-upgrade test, publication, live-signature, and live-system-test records. Keep private keys and credentials out of that evidence.

The existing v0.3.0 screenshots show actual application dialogs. They do not demonstrate Pacman trust setup, repository publication, or an operating-system upgrade, and should not be used as evidence for those checks.
