#!/usr/bin/env python3
"""SmartTar v0.2.0 reproducible command-line resource benchmark."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
from pathlib import Path
import platform
import random
import resource
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile


VERSION = "0.2.0"


def make_archive(path: Path, count: int, payload: bytes) -> dict:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                for index in range(count):
                    member = tarfile.TarInfo(f"files/{index:05d}.bin")
                    member.size = len(payload)
                    member.mode = 0o644
                    archive.addfile(member, io.BytesIO(payload))
    return {
        "members": count,
        "payload_bytes": count * len(payload),
        "compressed_bytes": path.stat().st_size,
    }


def measure_worker(arguments: list[str]) -> int:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    start = time.perf_counter()
    result = subprocess.run(arguments, capture_output=True, text=True, env=env)
    elapsed = time.perf_counter() - start
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(json.dumps({
        "elapsed_seconds": round(elapsed, 6),
        "max_rss_kib": int(usage.ru_maxrss),
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }))
    return 0


def measure(command: list[str]) -> dict:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "_measure", *command],
        capture_output=True,
        text=True,
        check=True,
    )
    measurement = json.loads(result.stdout)
    if measurement["returncode"]:
        raise RuntimeError(f"Benchmark command failed: {measurement['stderr']}")
    return measurement


def summarize(runs: list[dict]) -> dict:
    return {
        "median_seconds": round(statistics.median(run["elapsed_seconds"] for run in runs), 6),
        "peak_rss_kib": max(run["max_rss_kib"] for run in runs),
        "runs": [
            {key: run[key] for key in ("elapsed_seconds", "max_rss_kib")}
            for run in runs
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, default=Path(__file__).resolve().parents[1] / "smarttar.py")
    parser.add_argument("--compare-zip", type=Path, help="Also benchmark smarttar-cachyos/smarttar.py from a trusted previous release")
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("This benchmark reports Linux ru_maxrss values in KiB.")
    if not 1 <= args.repetitions <= 10:
        parser.error("Choose 1 to 10 repetitions.")

    with tempfile.TemporaryDirectory(prefix="smarttar-benchmark-") as temporary:
        workspace = Path(temporary)
        sources = {"current": args.core.resolve(strict=True)}
        if args.compare_zip:
            with zipfile.ZipFile(args.compare_zip) as archive:
                member = archive.getinfo("smarttar-cachyos/smarttar.py")
                if member.file_size > 1024 * 1024:
                    parser.error("Baseline source is unexpectedly larger than 1 MiB.")
                baseline = workspace / "baseline.py"
                baseline.write_bytes(archive.read(member))
                sources["baseline"] = baseline

        generator = random.Random(120)
        cases = {}
        for name, count, payload in (
            ("64_mib", 64, generator.randbytes(1024 * 1024)),
            ("5000_files", 5000, generator.randbytes(1024)),
        ):
            archive_path = workspace / f"{name}.tar.gz"
            cases[name] = (archive_path, make_archive(archive_path, count, payload))

        output = {
            "benchmark_version": VERSION,
            "environment": {
                "platform": platform.platform(),
                "architecture": platform.machine(),
                "python": platform.python_version(),
                "python_executable": sys.executable,
                "cache_state": "Filesystem caches are not flushed between runs",
            },
            "notes": [
                "Fixtures are generated before measurements with bounded memory",
                "Each run measures a fresh SmartTar CLI process",
                "Time includes CLI startup and shutdown",
                "inspect_then_extract includes the inspection required by the CLI",
                "KDialog, KDE, and filesystem cache memory are excluded",
                "No archive content is executed by these benchmark cases",
            ],
            "fixtures": {name: details for name, (_, details) in cases.items()},
            "results": {},
        }
        for label, core in sources.items():
            startup_runs = [measure([sys.executable, str(core), "--version"]) for _ in range(args.repetitions)]
            report = {
                "version": startup_runs[0]["stdout"],
                "source_bytes": core.stat().st_size,
                "startup": summarize(startup_runs),
                "cases": {},
            }
            for name, (archive_path, _) in cases.items():
                inspection_runs = []
                extraction_runs = []
                for index in range(args.repetitions):
                    inspection_runs.append(measure([sys.executable, str(core), "--inspect", str(archive_path)]))
                    destination = workspace / f"{label}-{name}-{index}"
                    destination.mkdir()
                    extraction_runs.append(measure([
                        sys.executable, str(core), str(archive_path), "--extract", str(destination),
                    ]))
                    shutil.rmtree(destination)
                report["cases"][name] = {
                    "inspect": summarize(inspection_runs),
                    "inspect_then_extract": summarize(extraction_runs),
                }
            output["results"][label] = report
        print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "_measure":
        raise SystemExit(measure_worker(sys.argv[2:]))
    raise SystemExit(main())
