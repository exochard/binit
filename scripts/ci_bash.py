#!/usr/bin/env python3
"""Build a checksum-pinned Bash and its matching headers for CI."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import urllib.request


SOURCES = {
    "5.2": ("5.2.15", "13720965b5f4fc3a0d4b61dd37e7565c741da9a5be24edc2ae00182fc1b3588c"),
    "5.3": ("5.3", "0d5cd86965f869a26cf64f4b71be7b96f90a3ba8b3d74e27e8e9d9d5550f31ba"),
}
MAX_ARCHIVE = 20 * 1024 * 1024


def download(version, destination):
    archive_version, checksum = SOURCES[version]
    url = f"https://ftp.gnu.org/gnu/bash/bash-{archive_version}.tar.gz"
    request = urllib.request.Request(url, headers={"User-Agent": "binit-ci"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Bash source archive exceeds 20 MiB")
    if hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError(f"Bash {archive_version} source checksum mismatch")
    destination.write_bytes(data)
    return url, checksum


def run(args, cwd):
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(args, cwd=cwd, check=True,
                   env={**os.environ, "CFLAGS": "-O2 -g -std=gnu17"})


def build(version, destination):
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"destination already exists: {destination}")
    destination.mkdir(parents=True)
    archive = destination / "source.tar.gz"
    url, checksum = download(version, archive)
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(destination, filter="data")
    source_dir = destination / f"bash-{SOURCES[version][0]}"
    prefix = destination / "install"
    run(["./configure", f"--prefix={prefix}", "--without-bash-malloc"], source_dir)
    run(["make", "-j2"], source_dir)
    run(["make", "install-headers"], source_dir)
    binary = prefix / "bin/bash"
    binary.parent.mkdir(parents=True, exist_ok=True)
    run(["install", "-m", "755", source_dir / "bash", binary], source_dir)
    headers = prefix / "include/bash"
    if not (headers / "config.h").is_file():
        raise ValueError(f"Bash headers not installed: {headers}")
    readline_headers = source_dir / "lib"
    if not (readline_headers / "readline/readline.h").is_file():
        raise ValueError(f"bundled Readline headers missing: {readline_headers}")
    info = {"version": version, "source_url": url, "source_sha256": checksum,
            "binary": str(binary), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "headers": str(headers), "readline_headers": str(readline_headers),
            "cflags": "-O2 -g -std=gnu17",
            "bash_version": subprocess.check_output([binary, "--version"], text=True).splitlines()[0]}
    (destination / "build.json").write_text(json.dumps(info, indent=2) + "\n")
    print(json.dumps(info, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", choices=sorted(SOURCES))
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    build(args.version, args.destination)


if __name__ == "__main__":
    main()
