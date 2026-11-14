#!/usr/bin/env python3
"""Create a deterministic archive from a clean independent plugin repository."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tomllib


def git_read(source, args, limit):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    with subprocess.Popen(["git", "-C", str(source), *args], env=environment,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as child:
        data = child.stdout.read(limit + 1)
        if len(data) > limit:
            child.kill()
            child.wait()
            raise ValueError("Git output exceeds the package limits")
        if child.wait():
            raise ValueError("cannot read committed plugin source")
        return data


def package(source, output):
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve()
    if source == output or source in output.parents:
        raise ValueError("output must be outside the plugin repository")
    top = Path(git_read(source, ["rev-parse", "--show-toplevel"], 16384).decode().strip()).resolve()
    if top != source:
        raise ValueError("source must be an independent repository root")
    if git_read(source, ["status", "--porcelain"], 1024 * 1024):
        raise ValueError("plugin repository has uncommitted files")
    commit = git_read(source, ["rev-parse", "HEAD"], 128).decode().strip()
    entries = git_read(source, ["ls-tree", "-rz", "--full-tree", commit], 1024 * 1024).split(b"\0")
    files = {}
    modes = {}
    total = 0
    for raw in entries:
        if not raw:
            continue
        metadata, raw_name = raw.split(b"\t", 1)
        mode, kind, object_id = metadata.decode().split()
        name = raw_name.decode("utf-8")
        parts = Path(name).parts
        if (mode not in ("100644", "100755") or kind != "blob"
                or not parts or Path(name).is_absolute() or len(parts) > 16
                or any(part in ("..", ".git") or part.startswith(".env") for part in parts)
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise ValueError(f"unsafe package file: {name}")
        size = int(git_read(source, ["cat-file", "-s", object_id], 32))
        total += size
        if len(files) >= 128 or total > 2 * 1024 * 1024:
            raise ValueError("plugin exceeds the source package limits")
        files[name] = git_read(source, ["cat-file", "blob", object_id], size)
        modes[name] = int(mode, 8) & 0o777
    manifest = tomllib.loads(files["plugin.toml"].decode())
    identifier = manifest["id"]
    if not identifier or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in identifier):
        raise ValueError("invalid plugin id")
    if manifest["entry"] not in files or "LICENSE" not in files:
        raise ValueError("entry and LICENSE must be tracked")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data in sorted(files.items()):
                item = tarfile.TarInfo(name)
                item.size = len(data)
                item.mode = modes[name]
                item.mtime = 0
                archive.addfile(item, io.BytesIO(data))
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"{identifier}-{commit[:12]}.tar.gz"
    data = buffer.getvalue()
    with artifact.open("xb") as stream:
        stream.write(data)
    receipt = {"id": identifier, "commit": commit, "artifact": artifact.name,
               "sha256": hashlib.sha256(data).hexdigest(),
               "modes": dict(sorted(modes.items())),
               "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    with artifact.with_suffix(".json").open("x") as stream:
        stream.write(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(package(args.source, args.output), indent=2))
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"package-plugin: {error}\n")
