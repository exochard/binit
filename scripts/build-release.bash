#!/usr/bin/env bash
set -euo pipefail
umask 022

if (( $# != 2 )) || [[ $1 != /* || $2 != /* ]]; then
    printf 'usage: build-release.bash SOURCE_DIR NEW_BUILD_DIR\n' >&2
    exit 2
fi
cd -- "$1"
build=$2
mkdir -- "$build"
export CARGO_HOME="$build/cargo"
export CARGO_TARGET_DIR="$build/target"
export CARGO_BUILD_JOBS=2
export BINIT_RELEASE_SOURCE="$PWD" BINIT_RELEASE_BUILD="$build"
export CC="$build/cc"
cat > "$CC" <<'CC'
#!/usr/bin/env bash
exec gcc "-ffile-prefix-map=$BINIT_RELEASE_SOURCE=/usr/src/binit" \
    "-ffile-prefix-map=$BINIT_RELEASE_BUILD=/usr/src/binit-build" \
    "-ffile-prefix-map=${BINIT_SYNTAX_SOURCE:-$BINIT_RELEASE_BUILD/grammar}=/usr/src/tree-sitter-bash" \
    "$@"
CC
chmod 0755 "$CC"
export RUSTFLAGS="--remap-path-prefix=$PWD=/usr/src/binit --remap-path-prefix=$build=/usr/src/binit-build"
export SOURCE_DATE_EPOCH=0

python3 -m venv "$build/venv"
export PATH="$build/venv/bin:$CARGO_HOME/bin:$PATH"
python3 -m pip install -r requirements-dev.txt
python3 scripts/ci_bash.py 5.2 "$build/bash-5.2"
python3 scripts/ci_bash.py 5.3 "$build/bash-5.3"
export BINIT_BASH_INCLUDE="$build/bash-5.3/install/include/bash"
export BINIT_READLINE_INCLUDE="$build/bash-5.3/bash-5.3/lib"
export BINIT_BASH_52_INCLUDE="$build/bash-5.2/install/include/bash"
export BINIT_READLINE_82_INCLUDE="$build/bash-5.2/bash-5.2.15/lib"
export BINIT_SYNTAX_RUNTIME="$build/tree-sitter"
if [[ -n ${BINIT_SYNTAX_SOURCE-} ]]; then
    python3 scripts/fetch_syntax.py --runtime-prefix "$BINIT_SYNTAX_RUNTIME"
else
    cargo install --locked tree-sitter-cli --version 0.26.9
    export BINIT_SYNTAX_SOURCE="$build/grammar"
    python3 scripts/fetch_syntax.py "$BINIT_SYNTAX_SOURCE" --runtime-prefix "$BINIT_SYNTAX_RUNTIME"
fi
cargo fetch --locked
cargo build --release --locked --offline

binary=$CARGO_TARGET_DIR/release/binit
version=$("$binary" --version)
version=${version#binit }
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
arch=$(uname -m)
archive=binit-$version-linux-$arch
stage=$build/$archive
mkdir -p -- "$stage/bin" "$build/dist"
install -m 0755 "$binary" "$stage/bin/binit"
install -m 0644 LICENSE "$stage/LICENSE"
install -m 0644 scripts/install.py "$stage/install.py"
install -m 0644 docs/install.md "$stage/INSTALL.md"

mapfile -t modules < <(find "$CARGO_TARGET_DIR/release/build" -name 'completion*.so' -type f -print)
test "${#modules[@]}" -eq 2
readelf --version-info "$stage/bin/binit" "${modules[@]}" > "$build/glibc-symbols.txt"
floor=$(grep -oE 'GLIBC_[0-9.]+' "$build/glibc-symbols.txt" | sort -Vu | tail -n 1)
if [[ $(printf '%s\n' GLIBC_2.34 "$floor" | sort -V | tail -n 1) != GLIBC_2.34 ]]; then
    printf 'Release requires %s\n' "$floor" >&2
    exit 1
fi

cargo metadata --locked --offline --format-version 1 \
    --filter-platform "$(rustc -vV | sed -n 's/^host: //p')" > "$build/cargo-metadata.json"
python3 scripts/release_notices.py "$build/cargo-metadata.json" "$stage" \
    "$BINIT_SYNTAX_SOURCE" "$BINIT_SYNTAX_RUNTIME/source" "$(rustc --print sysroot)"
gnu=binit-$version-gnu-sources
mkdir -- "$build/$gnu"
install -m 0644 "$build/bash-5.2/source.tar.gz" "$build/$gnu/bash-5.2.15.tar.gz"
install -m 0644 "$build/bash-5.3/source.tar.gz" "$build/$gnu/bash-5.3.tar.gz"
install -m 0644 LICENSES/GPL-3.0-or-later.txt "$build/$gnu/COPYING"
install -m 0644 scripts/ci_bash.py "$build/$gnu/build-bash.py"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -C "$build" \
    -cf - "$archive" | gzip -n > "$build/dist/$archive.tar.gz"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -C "$build" \
    -cf - "$gnu" | gzip -n > "$build/dist/$gnu.tar.gz"
(
    cd "$build/dist"
    sha256sum "$archive.tar.gz" "$gnu.tar.gz" > SHA256SUMS
    sha256sum -c SHA256SUMS
)

# Exercise the extracted archive and the installer on the target architecture.
mkdir -p "$build/unpacked" "$build/smoke-home"
tar -xzf "$build/dist/$archive.tar.gz" -C "$build/unpacked"
package=$build/unpacked/$archive
HOME="$build/smoke-home" python3 -I "$package/install.py" --binary "$package/bin/binit"
export BINIT_TEST_BINARY="$build/smoke-home/.local/bin/binit"
test "$("$BINIT_TEST_BINARY" --version)" = "binit $version"
export BINIT_EXPECT_HIGHLIGHT=1
state=$build/smoke-home/state
"$BINIT_TEST_BINARY" init --state "$state" > /dev/null
generation=$state/generations/$(cat "$state/current")
for abi in 5.2 5.3; do
    export BINIT_BASH="$build/bash-$abi/install/bin/bash"
    if [[ $abi == 5.2 ]]; then
        export BINIT_TEST_MODULE="$generation/completion-5.2.so"
    else
        export BINIT_TEST_MODULE="$generation/completion.so"
    fi
    python3 scripts/terminal.py smoke
    python3 -m unittest discover -s tests -p test_completion.py -k verified_generation
    python3 -m unittest discover -s tests -p test_editor.py
done
