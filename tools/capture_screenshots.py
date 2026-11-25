#!/usr/bin/env python3
"""SmartTar v0.4.0 real KDialog screenshot capture by Haytam Elbouhmidi.

I capture actual SmartTar windows in a private Xvfb/Openbox test session.
The archive and AppImage header fixtures are synthetic and never executed.
Dialog responses are supplied by this helper after each real window is captured.
These images are not native CachyOS, KDE Plasma, or DEsk compatibility tests.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


VERSION = "0.4.0"
REPOSITORY = Path(__file__).resolve().parents[1]
SCREENSHOTS = (
    ("--menu", "01-main-menu.png", "install\n", "Choose what to do with a downloaded archive."),
    ("--yesno", "02-install-confirmation.png", "", "Review the application and destination before installing."),
    ("--msgbox", "03-install-complete.png", "", "The example application has been copied into the private test directory."),
    ("--menu", "04-appimage-menu.png", "install\n", "Choose to install the synthetic AppImage header fixture."),
    ("--yesno", "05-appimage-confirmation.png", "", "Review the AppImage copy and application-menu shortcut before installing."),
    ("--msgbox", "06-appimage-complete.png", "", "The synthetic AppImage header fixture has been copied without running it."),
)
REQUIRED_TOOLS = (
    "kdialog", "Xvfb", "xvfb-run", "xauth", "openbox", "import", "identify", "xdotool", "dbus-run-session",
)
PROVENANCE = (
    "Actual SmartTar KDialog windows captured in an automated Linux Xvfb/Openbox session "
    "using a harmless example archive and a synthetic AppImage header fixture. "
    "Responses were supplied by the capture helper. Neither fixture was executed. "
    "The AppImage fixture is not a complete publisher AppImage or a runtime compatibility test. "
    "Appearance may differ on CachyOS, KDE Plasma, or DEsk."
)


class CaptureError(Exception):
    pass


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, timeout=5, check=False)
    if check and result.returncode:
        raise CaptureError(f"{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def stop_process(process: subprocess.Popen, *, group: bool = False) -> None:
    if process.poll() is not None:
        return
    try:
        if group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=3)
    except ProcessLookupError:
        process.wait(timeout=3)


def owned_window_details(process: subprocess.Popen) -> list[dict[str, str]]:
    result = command("xdotool", "search", "--all", "--pid", str(process.pid), check=False)
    details = []
    for window in result.stdout.split()[:8] if result.returncode == 0 else []:
        if not window.isdigit():
            continue
        owner = command("xdotool", "getwindowpid", window, check=False)
        if owner.returncode or owner.stdout.strip() != str(process.pid):
            continue
        title = command("xdotool", "getwindowname", window, check=False)
        geometry = command("xdotool", "getwindowgeometry", "--shell", window, check=False)
        details.append({
            "window": window,
            "title": title.stdout[:512].strip(),
            "geometry": geometry.stdout[:1024].strip(),
        })
    return details


def window_for(
    process: subprocess.Popen, title: str, diagnostics: dict[str, object] | None = None,
    *, timeout_seconds: float = 10,
) -> str:
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics["expected_title"] = repr(title)
    diagnostics["controlled_pid"] = str(process.pid)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CaptureError("KDialog exited before its window could be captured.")
        result = command(
            "xdotool", "search", "--all", "--onlyvisible", "--pid", str(process.pid),
            "--name", "^" + re.escape(title) + "($|[[:space:]])", check=False,
        )
        matches = result.stdout.split() if result.returncode == 0 else []
        diagnostics["last_search"] = (
            f"exit {result.returncode}, matches {len(matches)}, stderr {result.stderr[-1024:]!r}"
        )
        if len(matches) > 1:
            raise CaptureError("More than one window matched the controlled KDialog process.")
        if matches:
            window = matches[0]
            if not window.isdigit():
                raise CaptureError("The X11 window identifier is invalid.")
            if command("xdotool", "getwindowpid", window).stdout.strip() != str(process.pid):
                raise CaptureError("The window does not belong to the controlled KDialog process.")
            geometry = command("xdotool", "getwindowgeometry", "--shell", window).stdout
            values = dict(line.split("=", 1) for line in geometry.splitlines() if "=" in line)
            width = int(values.get("WIDTH", "0"))
            height = int(values.get("HEIGHT", "0"))
            window_title = command("xdotool", "getwindowname", window, check=False)
            diagnostics["last_owned_window"] = (
                f"id {window}, geometry {geometry[:1024]!r}, "
                f"title {window_title.stdout[:512].strip()!r}, title query exit {window_title.returncode}"
            )
            diagnostics["accepted_geometry"] = "width 120..1440, height 70..1000"
            if 120 <= width <= 1440 and 70 <= height <= 1000:
                time.sleep(0.4)
                if process.poll() is not None:
                    raise CaptureError("KDialog exited before painting completed.")
                return window
        time.sleep(0.1)
    diagnostics["owned_window_probe"] = owned_window_details(process)
    raise CaptureError(f"No acceptable visible KDialog window appeared within {timeout_seconds:g} seconds.")


def log_tail(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 4096))
            lines = stream.read(4096).decode("utf-8", errors="replace").splitlines()
        return "\n".join(lines[-12:]) or "(empty)"
    except OSError as exc:
        return f"(unavailable: {exc})"


def wait_for_manager(process: subprocess.Popen, session: Path) -> None:
    deadline = time.monotonic() + 10
    state = "No desktop count has been reported."
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CaptureError(f"Openbox exited during startup: {log_tail(session / 'openbox.log')!r}")
        result = command("xdotool", "get_num_desktops", check=False)
        count = result.stdout.strip()
        if result.returncode == 0 and count.isdigit() and int(count) > 0:
            return
        state = f"exit {result.returncode}, output {result.stdout[-512:]!r}, error {result.stderr[-512:]!r}"
        time.sleep(0.1)
    raise CaptureError(f"Openbox did not become ready: {state}. Log tail: {log_tail(session / 'openbox.log')!r}")


def verify_png(path: Path) -> tuple[int, int]:
    if not path.is_file() or not 100 < path.stat().st_size < 32 * 1024 * 1024:
        raise CaptureError(f"The screenshot is missing or has an unexpected size: {path.name}")
    with path.open("rb") as image:
        header = image.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise CaptureError(f"The screenshot is not a PNG image: {path.name}")
    width, height = struct.unpack(">II", header[16:24])
    if not 120 <= width <= 1460 or not 70 <= height <= 1040:
        raise CaptureError(f"The screenshot dimensions are unexpected: {width} x {height}")
    colors = command("identify", "-format", "%k", str(path)).stdout.strip()
    if not colors.isdigit() or int(colors) < 16:
        raise CaptureError(f"The screenshot appears blank or unpainted: {path.name}")
    return width, height


def make_example(archive: Path) -> None:
    executable = Path("/usr/bin/true")
    payload = executable.read_bytes()
    if not payload.startswith(b"\x7fELF"):
        raise CaptureError("The trusted /usr/bin/true fixture is not a Linux ELF executable.")
    files = (
        ("Example-App/example-app", payload, 0o755),
        ("Example-App/README.txt", b"SmartTar v0.4.0 screenshot fixture. This example is not run.\n", 0o644),
    )
    with tarfile.open(archive, "w:gz") as stream:
        for name, contents, mode in files:
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            member.mode = mode
            stream.addfile(member, io.BytesIO(contents))


def make_appimage_example(image: Path) -> None:
    payload = bytearray(Path("/usr/bin/true").read_bytes())
    if len(payload) <= 64 or payload[:4] != b"\x7fELF":
        raise CaptureError("The trusted /usr/bin/true fixture is not a Linux ELF executable.")
    payload[8:11] = b"AI\x02"
    image.write_bytes(payload)
    image.chmod(0o644)


def capture_session(session: Path, output: Path) -> None:
    if not session.is_dir() or output != session / "screenshots":
        raise CaptureError("The capture worker requires its private session directory.")
    if not os.environ.get("DISPLAY") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise CaptureError("The isolated X11 and D-Bus sessions are not available.")
    sys.path.insert(0, str(REPOSITORY))
    import smarttar

    output.mkdir()
    archive = session / "Example-App.tar.gz"
    make_example(archive)
    appimage = session / "Example-AppImage.AppImage"
    make_appimage_example(appimage)
    appimage_digest = hashlib.sha256(appimage.read_bytes()).hexdigest()
    records = []
    title = f"SmartTar v{smarttar.VERSION}"
    install_root = session / "Applications"
    launcher_root = session / "Desktop-Entries"

    def capture_dialog(*args: str) -> subprocess.CompletedProcess[str]:
        index = len(records)
        if index >= len(SCREENSHOTS):
            raise CaptureError("SmartTar displayed an unexpected additional dialog.")
        option, filename, response, caption = SCREENSHOTS[index]
        stage = f"Dialog {index + 1}/{len(SCREENSHOTS)} ({filename})"
        if manager.poll() is not None:
            raise CaptureError(
                f"{stage}: the isolated Openbox window manager exited before capture.\n"
                f"Openbox log tail: {log_tail(session / 'openbox.log')!r}"
            )
        if not args or args[0] != option:
            raise CaptureError(f"{stage}: expected {option}, received {args!r}.")
        invocation = ["kdialog", "--title", title, *args]
        log_path = session / f"dialog-{index + 1}.log"
        diagnostics = {}
        print(f"Capturing {stage}", flush=True)
        with log_path.open("w", encoding="utf-8") as log:
            try:
                process = subprocess.Popen(invocation, stdout=log, stderr=log)
            except OSError as exc:
                raise CaptureError(f"{stage}: could not start KDialog: {exc}") from exc
            try:
                window = window_for(process, title, diagnostics, timeout_seconds=20 if index == 0 else 10)
                if command("xdotool", "getwindowpid", window).stdout.strip() != str(process.pid):
                    raise CaptureError("The controlled window changed before capture.")
                path = output / filename
                command("import", "-window", window, "-frame", str(path))
                width, height = verify_png(path)
                records.append({
                    "file": filename,
                    "caption": caption,
                    "fixture": "Synthetic archive" if index < 3 else "Synthetic AppImage header fixture",
                    "width": width,
                    "height": height,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                })
                print(f"Captured {filename}: {width} x {height}", flush=True)
            except (CaptureError, OSError, ValueError, subprocess.SubprocessError) as exc:
                stop_process(process)
                log.flush()
                window_details = json.dumps(diagnostics, ensure_ascii=True, sort_keys=True)
                raise CaptureError(
                    f"{stage}: {exc}\nWindow diagnostics: {window_details}\n"
                    f"Controlled KDialog log tail: {log_tail(log_path)!r}"
                ) from exc
            finally:
                stop_process(process)
        return subprocess.CompletedProcess(invocation, 0, response, "")

    with (session / "openbox.log").open("w", encoding="utf-8") as log:
        manager = subprocess.Popen(["openbox", "--sm-disable"], stdout=log, stderr=log)
        try:
            wait_for_manager(manager, session)
            with patch.object(smarttar, "dialog", side_effect=capture_dialog), \
                    patch.object(smarttar, "app_root", return_value=install_root), \
                    patch.object(smarttar, "application_dir", return_value=launcher_root):
                smarttar.run_gui(archive)
                smarttar.run_gui(appimage)
            if len(records) != len(SCREENSHOTS):
                raise CaptureError("Both three-dialog installation workflows must finish.")
            if not (install_root / "example-app/payload/Example-App/example-app").is_file():
                raise CaptureError("The example installation did not complete in the private directory.")
            if not (launcher_root / "smarttar-example-app.desktop").is_file():
                raise CaptureError("The example application launcher was not created.")
            installed_appimage = install_root / "example-appimage/payload" / appimage.name
            if not installed_appimage.is_file() or hashlib.sha256(installed_appimage.read_bytes()).hexdigest() != appimage_digest:
                raise CaptureError("The synthetic AppImage header fixture was not copied correctly.")
            if installed_appimage.stat().st_mode & 0o777 != 0o755:
                raise CaptureError("The installed AppImage copy does not have executable permissions.")
            if not (launcher_root / "smarttar-example-appimage.desktop").is_file():
                raise CaptureError("The synthetic AppImage header fixture launcher was not created.")
            if hashlib.sha256(appimage.read_bytes()).hexdigest() != appimage_digest or appimage.stat().st_mode & 0o777 != 0o644:
                raise CaptureError("The synthetic AppImage header fixture source changed during installation.")
            if manager.poll() is not None:
                raise CaptureError("The isolated Openbox window manager exited before completion.")
        finally:
            stop_process(manager)

    details = {
        "capture_helper_version": VERSION,
        "smarttar_version": smarttar.VERSION,
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "source_revision": os.environ.get("GITHUB_SHA"),
        "operating_system": platform.freedesktop_os_release().get("PRETTY_NAME", "Linux"),
        "display": "Dedicated Xvfb with Openbox and Qt Fusion style",
        "provenance": PROVENANCE,
        "fixture": "Synthetic Example-App.tar.gz containing a copy of /usr/bin/true, never executed",
        "appimage_fixture": (
            "Synthetic AppImage header fixture made from a copy of /usr/bin/true with an AI type 2 marker, "
            "never executed. Not a complete publisher AppImage or a runtime compatibility test."
        ),
        "responses": "Supplied by the helper after each actual window was captured",
        "native_cachyos_kde_desk_validation": False,
        "screenshots": records,
    }
    (output / "capture.json").write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")


def isolated_environment(session: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "SESSION_MANAGER", "QT_QPA_PLATFORMTHEME"):
        environment.pop(key, None)
    for key, directory in (
        ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "runtime"),
    ):
        path = session / directory
        path.mkdir(mode=0o700)
        environment[key] = str(path)
    environment.update({
        "SMARTTAR_CAPTURE_SESSION": str(session),
        "QT_QPA_PLATFORM": "xcb",
        "QT_STYLE_OVERRIDE": "Fusion",
        "XDG_CURRENT_DESKTOP": "OPENBOX",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    })
    return environment


def capture(output: Path) -> None:
    missing = [name for name in REQUIRED_TOOLS if shutil.which(name) is None]
    if missing:
        raise CaptureError("Missing screenshot-only tools: " + ", ".join(missing))
    if output.exists() or output.is_symlink():
        raise CaptureError("Choose a new output directory. Existing captures are not overwritten.")
    with tempfile.TemporaryDirectory(prefix="smarttar-capture-") as temporary:
        session = Path(temporary)
        environment = isolated_environment(session)
        invocation = [
            "xvfb-run", "--auto-servernum", "--server-args=-screen 0 1440x1000x24 -nolisten tcp",
            "dbus-run-session", "--", sys.executable, str(Path(__file__).resolve()), "--worker",
        ]
        process = subprocess.Popen(invocation, env=environment, start_new_session=True)
        try:
            if process.wait(timeout=60) != 0:
                raise CaptureError("The isolated screenshot session failed. No captures were published.")
        finally:
            stop_process(process, group=True)
        staged = session / "screenshots"
        expected = [filename for _, filename, _, _ in SCREENSHOTS] + ["capture.json"]
        if not all((staged / filename).is_file() for filename in expected):
            raise CaptureError("The screenshot session did not produce every required file.")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.mkdir()
        created = []
        try:
            for filename in expected:
                destination = output / filename
                with destination.open("xb") as target, (staged / filename).open("rb") as source:
                    created.append(destination)
                    shutil.copyfileobj(source, target)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            output.rmdir()
            raise
    print(f"Captured six real SmartTar dialogs in {output}")
    print(PROVENANCE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--output", type=Path, default=Path("artifacts/screenshots"), help="New output directory")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker:
            session_value = os.environ.get("SMARTTAR_CAPTURE_SESSION")
            if not session_value:
                raise CaptureError("The internal worker must be started by the isolated capture launcher.")
            session = Path(session_value)
            capture_session(session, session / "screenshots")
        else:
            capture(args.output.expanduser().resolve())
    except (CaptureError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Screenshot capture failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
