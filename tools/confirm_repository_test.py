#!/usr/bin/env python3
"""SmartTar v0.4.0 prompt-by-prompt confirmations for disposable CI only."""

import errno
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import time


VERSION = "0.4.0"
PROMPTS = (
    b"Trust this SmartTar update source and continue? [y/N]",
    b"Proceed with installation? [Y/n]",
)


def run_confirmations(command, prompts=PROMPTS, timeout=240, output=None):
    output = sys.stdout.buffer if output is None else output
    master, slave = pty.openpty()
    process = None
    answered = 0
    pending = b""
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        slave = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("The live repository test did not reach an expected confirmation in time.")
            ready, _, _ = select.select([master], [], [], min(1, remaining))
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    break
                output.write(chunk)
                output.flush()
                pending = (pending + chunk)[-16384:]
                if answered < len(prompts) and prompts[answered] in pending:
                    os.write(master, b"y\n")
                    answered += 1
                    pending = b""
            elif process.poll() is not None:
                break
        status = process.wait(timeout=max(1, deadline - time.monotonic()))
        if status or answered != len(prompts):
            raise RuntimeError(f"Live repository setup failed with exit status {status} after {answered} confirmations.")
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except ProcessLookupError:
                pass


def main():
    if (os.geteuid() != 0 or os.environ.get("SMARTTAR_DISPOSABLE_CONTAINER") != "1"
            or not (Path("/.dockerenv").is_file() or Path("/run/.containerenv").is_file())):
        raise SystemExit("Run this confirmation helper only in the disposable Arch release test container.")
    if len(sys.argv) != 3:
        raise SystemExit("Provide the checked repository setup helper and its public key.")
    run_confirmations(["/usr/bin/python3", sys.argv[1], "--key", sys.argv[2]])


if __name__ == "__main__":
    main()
