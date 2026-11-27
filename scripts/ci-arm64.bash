#!/usr/bin/env bash
set -euo pipefail
umask 077

if (( $# != 1 )) || [[ $1 != /* ]]; then
    printf 'usage: ci-arm64.bash NEW_WORK_DIRECTORY\n' >&2
    exit 2
fi
work=$1
mkdir -p -- "$(dirname -- "$work")"
mkdir -- "$work"
revision=$(git rev-parse HEAD)
image_pin=50e24e1fb79e89116e939d90aa7738f7af48c442fa1bc2510131e0b39c50348e
image=${BINIT_CI_ARM64_IMAGE:-$work/ubuntu-arm64.img}
if [[ -z ${BINIT_CI_ARM64_IMAGE-} ]]; then
    curl --fail --silent --show-error --location --retry 2 --max-time 300 \
        --max-filesize 750000000 --proto '=https' --proto-redir '=https' \
        https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-arm64.img \
        --output "$image"
fi
image=$(realpath -- "$image")
printf '%s  %s\n' "$image_pin" "$image" | sha256sum --check

firmware=${BINIT_CI_ARM64_FIRMWARE:-/usr/share/AAVMF/AAVMF_CODE.fd}
vars=${BINIT_CI_ARM64_VARS:-/usr/share/AAVMF/AAVMF_VARS.fd}
cp -- "$vars" "$work/vars.fd"
ssh-keygen -q -t ed25519 -N '' -f "$work/key"
{
    cat <<'CONFIG'
#cloud-config
ssh_pwauth: false
disable_root: true
users:
  - name: binit-ci
    shell: /bin/bash
    lock_passwd: true
    sudo: 'ALL=(ALL) NOPASSWD:ALL'
    ssh_authorized_keys:
CONFIG
    printf '      - %s\n' "$(cat "$work/key.pub")"
} > "$work/user-data"
printf 'instance-id: binit-ci-arm64\nlocal-hostname: binit-arm64\n' > "$work/meta-data"
xorriso -as mkisofs -quiet -volid cidata -joliet -rock \
    -output "$work/seed.iso" "$work/user-data" "$work/meta-data"
qemu-img create -q -f qcow2 -F qcow2 -b "$image" "$work/guest.qcow2" 12G
truncate -s 1G "$work/swap.raw"

runner_memory() {
    local metric
    for metric in memory.max memory.peak memory.events; do
        if [[ -r /sys/fs/cgroup/$metric ]]; then
            printf 'Runner %s:\n' "$metric"
            cat "/sys/fs/cgroup/$metric" || true
        fi
    done
}

port=22369
runner_memory
# Leave room in the runner cgroup for QEMU, its translation cache and SSH.
qemu-system-aarch64 -accel 'tcg,thread=multi,tb-size=64' -machine 'virt,gic-version=3' -cpu cortex-a72 \
    -smp 2 -m 1536 -display none -monitor none -serial "file:$work/console.log" \
    -drive "if=pflash,format=raw,readonly=on,file=$firmware" \
    -drive "if=pflash,format=raw,file=$work/vars.fd" \
    -drive "if=none,id=root,format=qcow2,cache=none,file=$work/guest.qcow2" -device virtio-blk-pci,drive=root \
    -drive "if=none,id=swap,format=raw,cache=none,file=$work/swap.raw" -device virtio-blk-pci,drive=swap,serial=binit-ci-swap \
    -drive "if=none,id=seed,format=raw,readonly=on,file=$work/seed.iso" -device virtio-blk-pci,drive=seed \
    -nic "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:$port-:22" -no-reboot \
    > "$work/qemu.log" 2>&1 &
guest=$!
cleanup() {
    local status=$?
    kill -TERM "$guest" 2>/dev/null || true
    wait "$guest" 2>/dev/null || true
    runner_memory >&2
    if (( status != 0 )); then
        tail -30 -- "$work/qemu.log" "$work/console.log" >&2 || true
    fi
    return "$status"
}
trap cleanup EXIT
ssh_guest=(ssh -F /dev/null -p "$port" -o BatchMode=yes -o ConnectTimeout=4
    -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$work/known_hosts"
    -o IdentitiesOnly=yes -i "$work/key" binit-ci@127.0.0.1)
ready=0
for _ in {1..90}; do
    if ! kill -0 "$guest" 2>/dev/null; then
        cat -- "$work/qemu.log" >&2
        exit 1
    fi
    if "${ssh_guest[@]}" true 2>/dev/null; then
        ready=1
        break
    fi
    sleep 5
done
if (( ! ready )); then
    tail -50 -- "$work/console.log" >&2
    exit 1
fi

git archive "$revision" | "${ssh_guest[@]}" 'mkdir src && tar -xf - -C src'
printf 'Checking source %s\n' "$revision"
"${ssh_guest[@]}" 'bash -s' <<'GUEST' | tee "$work/verify.log"
set -euo pipefail
test "$(uname -m)" = aarch64
uname -smr
cloud-init status --wait
swap=/dev/disk/by-id/virtio-binit-ci-swap
test -b "$swap"
sudo -n mkswap "$swap"
sudo -n swapon "$swap"
free -m
sudo -n apt-get -o Acquire::Retries=2 update -qq
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    gcc git make patch bison curl ca-certificates libncurses-dev python3-venv
cd src
curl --proto '=https' --tlsv1.2 --fail --silent --show-error https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --no-modify-path
python3 -m venv .venv
export PATH="$PWD/.venv/bin:$HOME/.cargo/bin:$PATH"
python3 -m pip install --disable-pip-version-check -r requirements-dev.txt
mkdir -p tmp
export TMPDIR="$PWD/tmp" CARGO_BUILD_JOBS=1 BINIT_NATIVE=1
python3 scripts/ci_bash.py 5.2 "$PWD/tmp/bash-5.2"
python3 scripts/ci_bash.py 5.3 "$PWD/tmp/bash-5.3"
export BINIT_BASH_INCLUDE="$PWD/tmp/bash-5.3/install/include/bash"
export BINIT_READLINE_INCLUDE="$PWD/tmp/bash-5.3/bash-5.3/lib"
export BINIT_BASH_52_INCLUDE="$PWD/tmp/bash-5.2/install/include/bash"
export BINIT_READLINE_82_INCLUDE="$PWD/tmp/bash-5.2/bash-5.2.15/lib"
cargo fetch --locked
cargo build --release --locked --offline
export BINIT_TEST_BINARY="$PWD/target/release/binit"
state="$PWD/tmp/module-state"
"$BINIT_TEST_BINARY" init --state "$state" >/dev/null
generation="$state/generations/$(cat "$state/current")"
for version in 5.2 5.3; do
    export BINIT_BASH="$PWD/tmp/bash-$version/install/bin/bash"
    export BINIT_TEST_MODULE="$generation/completion.so"
    if [[ $version == 5.2 ]]; then
        export BINIT_TEST_MODULE="$generation/completion-5.2.so"
    fi
    sha256sum -- "$BINIT_BASH" "$BINIT_TEST_MODULE"
    for suite in test_completion.py test_editor.py test_wait.py test_update_candidate.py; do
        python3 -m unittest discover -s tests -p "$suite"
    done
    python3 scripts/terminal.py smoke
done
GUEST
"${ssh_guest[@]}" 'sudo -n shutdown -h now' || true
status=0
wait "$guest" || status=$?
trap - EXIT
runner_memory
exit "$status"
