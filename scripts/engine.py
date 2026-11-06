#!/usr/bin/env python3
"""Build pinned GNU Bash interpreters without installing them on the system."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache/engines"
LOCK = ROOT / "engines/bash/source.json"
CONFIGURE = ["--without-bash-malloc"]
BASE_FLAGS = "-O2 -g"
OPT_FLAGS = "-O3 -g -flto=2"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_digest(path):
    entries = []
    for child in sorted(path.rglob("*")):
        name = child.relative_to(path).as_posix()
        if child.is_symlink():
            entries.append([name, "symlink", os.readlink(child)])
        elif child.is_file():
            entries.append([name, "file", child.stat().st_mode & 0o777, digest(child)])
        else:
            entries.append([name, "directory"])
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def run(args, cwd, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), cwd=cwd, check=True, **kwargs)


def source():
    lock = json.loads(LOCK.read_text())
    downloads = CACHE / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    for artifact in lock["files"]:
        path = downloads / artifact["name"]
        if not path.exists():
            request = urllib.request.Request(artifact["url"], headers={"User-Agent": "binit-build"})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(20 * 1024 * 1024 + 1)
            if len(data) > 20 * 1024 * 1024:
                raise ValueError(f"download too large: {artifact['name']}")
            path.write_bytes(data)
        if digest(path) != artifact["sha256"]:
            raise ValueError(f"checksum mismatch: {path}")
    key_home = downloads / "gnupg"
    key_home.mkdir(mode=0o700, exist_ok=True)
    for artifact in lock["files"]:
        if artifact["name"].endswith(".sig"):
            signature = downloads / artifact["name"]
            result = run(["gpgv", "--homedir", key_home, "--keyring", downloads / "gnu-keyring.gpg",
                          "--status-fd", "1", signature, signature.with_suffix("")],
                         ROOT, capture_output=True, text=True)
            fingerprints = [line.split()[2] for line in result.stdout.splitlines()
                            if line.startswith("[GNUPG:] VALIDSIG ")]
            if not fingerprints or not set(fingerprints) <= set(lock["signing_fingerprints"]):
                raise ValueError(f"unexpected signer: {signature.name}")
    destination = CACHE / f"source-{lock['version']}"
    if not destination.exists():
        staging = CACHE / ".scratch-extract"
        staging.mkdir(exist_ok=False)
        with tarfile.open(downloads / "bash-5.3.tar.gz") as archive:
            archive.extractall(staging, filter="data")
        extracted = staging / "bash-5.3"
        for number in range(1, 21):
            run(["patch", "--batch", "--forward", "-p0", "-i", downloads / f"bash53-{number:03}"], extracted)
        extracted.rename(destination)
        staging.rmdir()
    expected = (ROOT / "engines/bash/tree.sha256").read_text().strip()
    if source_digest(destination) != expected:
        raise ValueError("cached source tree differs from the authenticated patched source")
    return destination, lock


def build(kind):
    source_dir, lock = source()
    directory = CACHE / kind
    directory.mkdir(exist_ok=True)
    binary = directory / "bash"
    metadata = directory / "build.json"
    if metadata.exists():
        raise ValueError(f"build already exists: {directory}; select a fresh build name")
    flags = BASE_FLAGS if kind in ("reference", "profile") else OPT_FLAGS
    if kind == "profile":
        flags += " -pg"
    profile = directory / "profile"
    if kind == "pgo":
        profile.mkdir(exist_ok=True)
        flags += f" -fprofile-generate={profile}"
    env = os.environ.copy()
    linker_flags = "-pg" if kind == "profile" else ("-flto=2" if kind != "reference" else "")
    env.update(CC="gcc", CFLAGS=flags, LDFLAGS=linker_flags)
    run([source_dir / "configure", *CONFIGURE, f"--prefix={directory / 'install'}"], directory, env=env)
    run(["make", "-j2"], directory, env=env)
    if kind == "pgo":
        run(["bash", ROOT / "engines/bash/train.bash", binary], ROOT, env=env)
        flags = OPT_FLAGS + f" -fprofile-use={profile} -fprofile-correction"
        # Recompile in the same directory so GCC resolves the generated profiles.
        # Removing only generated objects avoids rerunning configure or the parser generator.
        for obj in directory.rglob("*.o"):
            obj.unlink()
        for library in directory.rglob("*.a"):
            library.unlink()
        binary.unlink()
        # Keep Bash's configured linker flags, including exports for loadable builtins.
        run(["make", "-j2", f"CFLAGS={flags}"], directory)
    info = {"kind": kind, "version": lock["version"], "source_lock_sha256": digest(LOCK),
            "source_tree_sha256": source_digest(source_dir),
            "binary_sha256": digest(binary), "configure": CONFIGURE, "cflags": flags,
            "compiler": subprocess.check_output(["gcc", "--version"], text=True).splitlines()[0],
            "bash_version": subprocess.check_output([binary, "--version"], cwd=directory, text=True).splitlines()[0]}
    if kind == "pgo":
        info["training_sha256"] = digest(ROOT / "engines/bash/train.bash")
    metadata.write_text(json.dumps(info, indent=2) + "\n")
    print(json.dumps(info, indent=2))


def test(kind):
    directory = CACHE / kind
    if not (directory / "build.json").exists():
        raise ValueError("build the interpreter first")
    locale_dir = prepare_locales()
    with tempfile.TemporaryDirectory(prefix=".scratch-bash-tests-", dir=ROOT) as temporary:
        home = Path(temporary) / "home"
        home.mkdir(mode=0o700)
        tools = Path(temporary) / "bin"
        tools.mkdir()
        locale = tools / "locale"
        locale.write_text("#!/usr/bin/env python3\nimport os,sys\n"
                          "if sys.argv[1:] in (['-a'], ['--all-locales']):\n"
                          " import subprocess\n subprocess.run(['/usr/bin/locale', '-a'], check=True)\n"
                          f" for name in {sorted(p.name for p in locale_dir.iterdir())!r}: print(name)\n"
                          "else: os.execv('/usr/bin/locale', ['locale', *sys.argv[1:]])\n")
        locale.chmod(0o700)
        metadata = json.loads((directory / "build.json").read_text())
        command = shlex.join(["make", f"CFLAGS={metadata['cflags']}", "tests"])
        input_read, input_write = os.pipe()
        # Keep an idle terminal input open: EOF would invalidate read-timeout tests.
        with os.fdopen(input_read, "rb") as terminal_input, os.fdopen(input_write, "wb"):
            result = run(["script", "--quiet", "--return", "--command", command, "/dev/null"], directory,
                         env={**os.environ, "LC_ALL": "C", "TERM": "xterm", "COLUMNS": "80",
                              "HOME": str(home), "TMPDIR": temporary,
                              "LOCPATH": str(locale_dir), "PATH": str(tools) + os.pathsep + os.defpath},
                         stdin=terminal_input, capture_output=True, text=True, errors="replace")
        print(result.stdout)
        if result.stderr:
            print(result.stderr)
        # Upstream run-all exits zero even when an individual diff fails.
        if re.search(r"(?m)^\d+(?:,\d+)?[acd]\d+(?:,\d+)?\r?$", result.stdout):
            raise ValueError("upstream tests reported differences; inspect the test log")


def prepare_locales():
    directory = CACHE / "locales"
    directory.mkdir(exist_ok=True)
    for name, charmap in [("de_DE.UTF-8", "UTF-8"), ("en_US.UTF-8", "UTF-8"),
                          ("fr_FR.ISO8859-1", "ISO-8859-1"), ("ja_JP.SHIFT_JIS", "SHIFT_JIS"),
                          ("ja_JP.SJIS", "SHIFT_JIS"), ("ru_RU.CP1251", "CP1251"),
                          ("zh_HK.big5hkscs", "BIG5-HKSCS"), ("zh_TW.BIG5", "BIG5"), ("zh_TW.big5", "BIG5")]:
        if (directory / name / "LC_CTYPE").exists():
            continue
        result = subprocess.run(["localedef", "--no-archive", "-i", name.split(".")[0],
                                 "-f", charmap, str(directory / name)], capture_output=True, text=True)
        if result.returncode != 0 and not (result.returncode == 1 and (directory / name / "LC_CTYPE").exists()):
            raise ValueError(f"cannot prepare test locale {name}: {result.stderr.strip()}")
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["source", "build", "test"])
    parser.add_argument("kind", nargs="?", choices=["reference", "lto", "pgo", "profile"], default="reference")
    args = parser.parse_args()
    if args.command == "source":
        print(source()[0])
    elif args.command == "build":
        build(args.kind)
    else:
        test(args.kind)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
