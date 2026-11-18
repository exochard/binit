#!/usr/bin/env bash
# Usage: scripts/plugin_corpus.bash COMMIT RUST_ARCHIVE OUTDIR
# Stages the frozen corpus inputs with their pinned hashes, builds the pinned image for COMMIT and runs
# scripts/check_plugin_corpus.py inside it without network. RUST_ARCHIVE is the Rust 1.99.1 x86_64
# standalone installer archive also used by the source trial.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ $# == 3 ]] || { printf 'usage: %s COMMIT RUST_ARCHIVE OUTDIR\n' "$0" >&2; exit 2; }
commit=$(git rev-parse --verify "$1^{commit}")
out=$(realpath -m -- "$3")
[[ ! -e $out ]] || { printf '%s exists\n' "$out" >&2; exit 2; }
context=$out/context
mkdir -p -- "$context" "$out/run"
check() { printf '%s  %s\n' "$1" "$2" | sha256sum --check --quiet; }
fetch() { curl --proto '=https' --tlsv1.2 -fsSL "$1" -o "$2"; check "$3" "$2"; }
manifest=benchmarks/plugin-corpus.json
value() { python3 -c 'import functools,json,sys; print(functools.reduce(lambda d, k: d[k], sys.argv[2:], json.load(open(sys.argv[1]))))' "$manifest" "$@"; }
git archive --format=tar "$commit" > "$context/binit-source.tar"
printf '%s\n' "$commit" > "$context/binit-commit"
cp -- "$2" "$context/rust.tar.xz"
check 231fcae47ee950621374725b5fc3b5441d2fbda2955ac45dd7dc816d8876cfe7 "$context/rust.tar.xz"
tag=v0.75.0 fzf_commit=c7bf2b8eea25952dd257a466c0afa72faae206fb
fetch "https://github.com/junegunn/fzf/releases/download/$tag/fzf-0.75.0-linux_amd64.tar.gz" "$context/fzf.tar.gz" \
  "$(value plugins fzf binary_archive_sha256)"
mkdir "$context/fzf-shell"
for file in key-bindings.bash completion.bash; do
  fetch "https://raw.githubusercontent.com/junegunn/fzf/$fzf_commit/shell/$file" "$context/fzf-shell/$file" \
    "$(value plugins fzf shell_files_sha256 "shell/$file")"
done
for version in 0.40.2 0.40.3; do
  mkdir "$context/nvm-$version"
  fetch "https://raw.githubusercontent.com/nvm-sh/nvm/$(value plugins nvm versions "$version" commit)/nvm.sh" \
    "$context/nvm-$version/nvm.sh" "$(value plugins nvm versions "$version" nvm_sh_sha256)"
done
for framework in bash-it oh-my-bash; do
  repository=$(value frameworks "$framework" repository)
  revision=$(value frameworks "$framework" revision)
  git init -q "$context/$framework"
  git -C "$context/$framework" fetch -q --depth=1 "https://github.com/$repository.git" "$revision"
  git -C "$context/$framework" -c advice.detachedHead=false checkout -q FETCH_HEAD
  [[ $(git -C "$context/$framework" rev-parse HEAD) == "$revision" ]]
done
python3 - "$context" <<'PY' > "$context/inputs.json"
import hashlib, json, subprocess, sys
from pathlib import Path
c = Path(sys.argv[1])
files = {p.relative_to(c).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
         for p in sorted(c.rglob('*')) if p.is_file() and '.git' not in p.relative_to(c).parts}
revisions = {name: subprocess.check_output(['git', '-C', str(c / name), 'rev-parse', 'HEAD'], text=True).strip()
             for name in ('bash-it', 'oh-my-bash')}
print(json.dumps({'files': files, 'revisions': revisions}, indent=1, sort_keys=True))
PY
nice -n 19 docker build --iidfile "$out/image.id" -f benchmarks/plugin-corpus/Containerfile "$context" > "$out/build.log" 2>&1
docker run --rm --network none -v "$out/run:/out" "$(<"$out/image.id")" \
  nice -n 19 python3 scripts/check_plugin_corpus.py --work /tmp/corpus --output /out/receipt.json
