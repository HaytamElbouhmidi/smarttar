# SmartTar

**Install downloaded Linux apps with a few clicks.**

Version 0.3.0 · Testing · CachyOS and Arch Linux · KDE Plasma and DEsk

I built SmartTar for people who want to install a downloaded program without working out terminal commands. After the one-time setup, open a `.tar.gz`, `.tgz`, or `.AppImage` download with SmartTar, choose the install option, and confirm. The installed app appears in your application menu.

For example, if a Linux app's website gives you an unfamiliar download and you do not know what to do with it, SmartTar checks whether it recognizes a supported app. For a supported archive, it unpacks the program. For an AppImage, it saves a copy in a permanent location and makes a menu shortcut. You confirm before either installation begins.

**Download it. Open it. Install it.** No terminal commands are needed for ordinary use with supported app downloads.

[Download SmartTar](https://github.com/HaytamElbouhmidi/smarttar/archive/refs/heads/main.zip) · [Screenshots](#screenshots) · [One-time setup](#installation) · [Technical details](docs/TECHNICAL.md)

## What does it do?

A `.tar.gz` file is a bundle of files packed together to make the download smaller, similar to a ZIP. Some contain programs that are ready to use. Others contain documents, installer scripts, or source code, the files a developer uses to build a program. The word **archive** in SmartTar means this downloaded bundle.

A `.AppImage` file is a Linux app packaged as one file. SmartTar handles the usual setup for you, so you do not need to change its permissions yourself or remember where you left it in Downloads.

SmartTar looks inside the download and offers the options that fit:

- **Install an app:** Save a supported program on your computer for your own use and add a shortcut to your application menu.
- **Unpack an archive:** Put the contents of a `.tar.gz` or `.tgz` file into a new folder.
- **Get guidance:** Show what to do when the download needs a different installation method.

It asks before making changes. Opening a download **with SmartTar** does not run its programs or scripts. SmartTar does not check whether a publisher is trustworthy, so only install downloads from sources you trust.

## Everyday use

### 1. Open your download

Download a Linux app from a website you trust.

- **`.tar.gz` or `.tgz`:** Double-click the file in Dolphin, the file manager.
- **`.AppImage`:** Right-click the file, choose **Open With**, and select **SmartTar**. You can also double-click when your desktop has SmartTar set as the handler.

Some desktops run an executable AppImage directly when you double-click it. Use **Open With SmartTar** when you want to install it instead. Both `.AppImage` and `.appimage` filenames are accepted.

### 2. Choose Install

For an AppImage, choose **Install AppImage**. SmartTar saves a copy in its own application folder, allows that copy to run, and creates a menu shortcut. The original download is left unchanged. You can remove the original from Downloads after confirming that the installed copy opens correctly.

For an archive containing a supported ready-to-run program, choose **Install portable application** or the named install option. Here, portable means the app can run from its own folder.

If an archive has no install option, choose **Extract to a new folder** or **Installation guidance**. AppImages are installed as a single file, not unpacked by SmartTar.

### 3. Confirm and open the app

Review the destination, the folder where the program will be saved, and confirm. Once installation finishes, open the app from your normal application menu, where you find your other programs. You do not need to keep SmartTar open.

If SmartTar lists several programs and you are unsure which one to choose, cancel and check the publisher's instructions.

Large archives can take time to check before the first window appears. AppImage inspection only reads a small file header, so it does not scan or unpack the whole app before showing its options. Copying a large AppImage can still take time. This version does not yet show a progress bar.

## Screenshots

These screenshots show SmartTar v0.3.0 installing an archive and an AppImage. Both flows ask for confirmation before installation.

### Choose what to do

Open a download and select an available install option, unpack its files, or get guidance.

![SmartTar showing the install, extract, review, and guidance options for an example app](docs/screenshots/01-main-menu.png)

### Review and confirm

Check which program will be installed and where it will be placed. Choose **Yes** only if you trust the download.

![SmartTar asking for confirmation before installing the example app](docs/screenshots/02-install-confirmation.png)

### Find the app in your menu

After installation, SmartTar confirms that the app has an entry in the application menu.

![SmartTar confirming successful installation and the application-menu entry](docs/screenshots/03-install-complete.png)

### Install an AppImage

Right-click the downloaded file, open it with SmartTar, and choose **Install AppImage**.

![SmartTar offering to install an example AppImage or explain how installation works](docs/screenshots/04-appimage-menu.png)

Review where the app will be saved and choose **Yes** if you trust the download. SmartTar copies the file and adds a menu shortcut without starting the app.

![SmartTar asking for confirmation before copying an AppImage and adding its menu shortcut](docs/screenshots/05-appimage-confirmation.png)

Open the new entry in your application menu when you are ready to use it. Your original download is still where you left it.

![SmartTar confirming that the AppImage is installed and the original download is unchanged](docs/screenshots/06-appimage-complete.png)

These are real SmartTar dialogs captured in an automated Ubuntu test session, not mockups. The example downloads are synthetic test files, so these images demonstrate the installer rather than a particular app's ability to run. Colors and window borders depend on your desktop theme. The temporary destination shown is only for the test. See the [capture details](docs/screenshots/capture.json).

## Installation

### One-time setup

**The current setup still requires two terminal commands.** You only do this to install SmartTar or update it. Everyday use is graphical after setup.

1. Use **Download SmartTar** above and extract the ZIP. Extract means unpacking its contents into a normal folder. Do not try to run the installer from inside the ZIP viewer.
2. Open the extracted folder containing `install.sh` in Dolphin.
3. Right-click inside the folder and choose **Open Terminal Here**. A terminal is a window where you enter commands. You can copy the two commands below as written.
4. Copy and paste this command to install any missing requirements. Enter your Linux password if asked:

   ```bash
   sudo pacman -S --needed python kdialog xdg-utils
   ```

5. Run the SmartTar installer:

   ```bash
   bash install.sh
   ```

   Do not put `sudo` before this second command. SmartTar installs for your own user account.

6. Close the terminal and double-click `examples/smarttar-smoke-test.tar.gz` in the extracted project folder. SmartTar should open and offer to unpack its text file.

If setup reports a warning or opening an archive still starts Ark, see [troubleshooting](docs/TECHNICAL.md#diagnostics-and-command-line-extraction).

### Updating an earlier installation

Download and extract the latest SmartTar ZIP, open a terminal in that new folder, and run `bash install.sh` again without `sudo`. This updates SmartTar and adds its AppImage file associations. Your already installed apps are kept.

Close and reopen Dolphin afterward. To check the installed version, run:

```bash
python3 ~/.local/share/smarttar/smarttar.py --version
```

It should report `0.3.0`.

### Try it with an AppImage

1. Download an AppImage for your computer from the app's official website.
2. Right-click it in Dolphin and select **Open With SmartTar**.
3. Choose **Install AppImage**, review the destination, and confirm.
4. Open your application menu and launch the new entry.
5. Confirm that the original file is still in Downloads and the menu entry opens the installed copy.

After the one-time setup, these steps do not require a terminal. If SmartTar reports the wrong processor type, download the version that matches your computer. If installation succeeds but the app does not open, check the publisher's Linux requirements and [AppImage troubleshooting](docs/TECHNICAL.md#appimage-troubleshooting).

## What if there is no Install button?

Not every compressed download contains a ready-to-run app. Some need to be built from source or installed through another tool. SmartTar can unpack those files and offer guidance, but it does not silently run unfamiliar installers.

It also does not automatically download missing libraries or make a Windows program run on Linux. The publisher's requirements still apply.

SmartTar recognizes standard AppImages with a supported file header. A renamed file, an invalid header, or an older AppImage without that header is not accepted just because its name ends in `.AppImage`.

## Is it lightweight?

Yes. The installer copies one small Python program, using the Python and KDE components already on the system when available.

- No background service, tray app, or startup process.
- No bundled browser or Python runtime.
- Nothing stays running when SmartTar is closed.
- An installed app opens directly, without starting SmartTar again.

## Why are there checks?

The automated tests on GitHub run on GitHub's computers, not on yours. They are not run when you open a download. Tests, screenshots, and this documentation are not copied into the installed app.

SmartTar does check each archive before installation and while unpacking it. This helps catch damaged files, unsafe paths, and accidental overwrites. These checks add some processing time for large downloads, but there is no background scanning.

AppImages take a shorter route. SmartTar checks the file header and processor type, then copies the file once when you confirm. It checks for changes during that same copy, without a separate full-file scan. It does not run the AppImage to inspect it or extract an icon. The menu entry uses a generic application icon.

See the [measurements and technical details](docs/TECHNICAL.md#performance-measurements) if you want the numbers.

## Current status

This is a testing release, so you may still run into problems. Automated checks do not replace testing on real CachyOS, KDE Plasma, and DEsk setups. SmartTar can install programs it recognizes as ready to use, not every possible archive or AppImage. A successful installation does not guarantee that the app has all the system libraries it needs to run.

The detailed compatibility list, size limits, diagnostics, benchmark results, and removal instructions are in [the technical guide](docs/TECHNICAL.md).

## License

MIT license. Copyright (c) 2026 Haytam Elbouhmidi, Mechanical Engineer.

See [LICENSE](LICENSE) and [CHANGELOG.md](CHANGELOG.md).
