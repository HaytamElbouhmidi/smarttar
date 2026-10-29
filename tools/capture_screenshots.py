#!/usr/bin/env python3
"""SmartTar v0.2.0 real KDialog screenshot capture by Haytam Elbouhmidi.

I capture actual SmartTar windows in a private Xvfb/Openbox test session.
The example archive is synthetic and no application from it is executed.
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


VERSION = "0.2.0"
REPOSITORY = Path(__file__).resolve().parents[1]
SCREENSHOTS = (
    ("--menu", "01-main-menu.png", "install\n", "Choose what to do with a downloaded archive."),
    ("--yesno", "02-install-confirmation.png", "", "Review the application and destination before installing."),
    ("--msgbox", "03-install-complete.png", "", "The example application has been copied into the private test directory."),
)
REQUIRED_TOOLS = (
    "kdialog", "Xvfb", "xvfb-run", "xauth", "openbox", "import", "identify", "xdotool", "dbus-run-session",
)
PROVENANCE = (
    "Actual SmartTar KDialog windows captured in an automated Linux Xvfb/Openbox session "
    "using a harmless example archive. Responses were supplied by the capture helper. "
    "No application from the archive was run. Appearance may differ on CachyOS, KDE Plasma, or DEsk."
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


def window_for(process: subprocess.Popen, title: str) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CaptureError("KDialog exited before its window could be captured.")
        result = command(
            "xdotool", "search", "--all", "--onlyvisible", "--pid", str(process.pid),
            "--name", "^" + re.escape(title) + "($|[[:space:]])", check=False,
        )
        matches = result.stdout.split() if result.returncode == 0 else []
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
            if 120 <= width <= 1440 and 70 <= height <= 1000:
                time.sleep(0.4)
                if process.poll() is not None:
                    raise CaptureError("KDialog exited before painting completed.")
                return window
        time.sleep(0.1)
    raise CaptureError("No visible KDialog window appeared within 10 seconds.")


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
        ("Example-App/README.txt", b"SmartTar v0.2.0 screenshot fixture. This example is not run.\n", 0o644),
    )
    with tarfile.open(archive, "w:gz") as stream:
        for name, contents, mode in files:
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            member.mode = mode
            stream.addfile(member, io.BytesIO(contents))


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
    records = []
    title = f"SmartTar v{smarttar.VERSION}"
    install_root = session / "Applications"
    launcher_root = session / "Desktop-Entries"

    def capture_dialog(*args: str) -> subprocess.CompletedProcess[str]:
        index = len(records)
        if index >= len(SCREENSHOTS):
            raise CaptureError("SmartTar displayed an unexpected additional dialog.")
        option, filename, response, caption = SCREENSHOTS[index]
        if not args or args[0] != option:
            raise CaptureError(f"Expected {option}, received {args!r}.")
        invocation = ["kdialog", "--title", title, *args]
        with (session / f"dialog-{index + 1}.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(invocation, stdout=log, stderr=log)
            try:
                window = window_for(process, title)
                if command("xdotool", "getwindowpid", window).stdout.strip() != str(process.pid):
                    raise CaptureError("The controlled window changed before capture.")
                path = output / filename
                command("import", "-window", window, "-frame", str(path))
                width, height = verify_png(path)
                records.append({
                    "file": filename,
                    "caption": caption,
                    "width": width,
                    "height": height,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                })
            finally:
                stop_process(process)
        return subprocess.CompletedProcess(invocation, 0, response, "")

    with (session / "openbox.log").open("w", encoding="utf-8") as log:
        manager = subprocess.Popen(["openbox", "--sm-disable"], stdout=log, stderr=log)
        try:
            with patch.object(smarttar, "dialog", side_effect=capture_dialog), \
                    patch.object(smarttar, "app_root", return_value=install_root), \
                    patch.object(smarttar, "application_dir", return_value=launcher_root):
                smarttar.run_gui(archive)
            if len(records) != len(SCREENSHOTS):
                raise CaptureError("The full three-dialog workflow did not finish.")
            if not (install_root / "example-app/payload/Example-App/example-app").is_file():
                raise CaptureError("The example installation did not complete in the private directory.")
            if not (launcher_root / "smarttar-example-app.desktop").is_file():
                raise CaptureError("The example application launcher was not created.")
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
    print(f"Captured three real SmartTar dialogs in {output}")
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
