#!/usr/bin/env python3
"""Install or remove the user-owned Binit executable."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
START = b'# >>> binit >>>\n'
END = b'# <<< binit <<<\n'


def absolute(path):
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def check_path(path):
    path = absolute(path)
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f'symlink in path: {part}')
        if info.st_uid not in (0, os.getuid()):
            raise ValueError(f'foreign owner: {part}')
    return path


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f'expected an owned regular file: {path}')
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


def atomic(path, data, mode):
    check_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    check_path(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.binit-install-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def backup(path, data, mode):
    name = path.with_name(f'{path.name}.binit-{time.time_ns()}.bak')
    if name.exists() or name.is_symlink():
        raise ValueError(f'backup already exists: {name}')
    atomic(name, data, mode)


def load_receipt(path, binary):
    if not path.exists():
        if binary.exists() or binary.is_symlink():
            raise ValueError(f'existing executable is not recorded: {binary}')
        return None
    data, _ = regular(path)
    receipt = json.loads(data)
    if not isinstance(receipt, dict) or set(receipt) != {'schema', 'binary', 'sha256', 'mode', 'rc_file', 'rc_block'}:
        raise ValueError('invalid installation receipt')
    checksum = receipt['sha256']
    rc_file, encoded = receipt['rc_file'], receipt['rc_block']
    if (receipt['schema'] != 1 or receipt['binary'] != str(binary) or receipt['mode'] != 0o755
            or not isinstance(checksum, str) or len(checksum) != 64
            or any(char not in '0123456789abcdef' for char in checksum)
            or (rc_file is None) != (encoded is None)):
        raise ValueError('invalid installation receipt')
    if rc_file is not None:
        if not isinstance(rc_file, str) or not Path(rc_file).is_absolute() or not isinstance(encoded, str):
            raise ValueError('invalid installation receipt')
        block = base64.b64decode(encoded, validate=True)
        if not (block.startswith(START) or block.startswith(b'\n' + START)) or not block.endswith(END):
            raise ValueError('invalid installation receipt')
    current, mode = regular(binary)
    if digest(current) != receipt['sha256'] or mode != receipt['mode']:
        raise ValueError(f'installed executable changed: {binary}')
    return receipt


def decode_block(receipt):
    value = receipt.get('rc_block')
    return base64.b64decode(value, validate=True) if value else b''


def startup(binary):
    return f'eval "$({shlex.quote(str(binary))} init)"'


def rc_update(path, old_block, line):
    check_path(path)
    existed = path.exists()
    original, mode = regular(path) if existed else (b'', 0o600)
    marker = START + line.encode() + b'\n' + END
    if old_block:
        if original.count(old_block) != 1:
            raise ValueError(f'installed startup block changed: {path}')
        new_block = (b'\n' if old_block.startswith(b'\n') else b'') + marker
        updated = original.replace(old_block, new_block, 1)
    else:
        if START in original or END in original:
            raise ValueError(f'unrecorded startup block: {path}')
        new_block = (b'\n' if original and not original.endswith(b'\n') else b'') + marker
        updated = original + new_block
    return existed, original, mode, updated, new_block


def install(binary_source, prefix, rc_path=None):
    source = check_path(binary_source)
    source_data, source_mode = regular(source)
    if not source_data:
        raise ValueError('source executable is empty')
    if not source_mode & 0o111:
        raise ValueError('source executable is not executable')
    prefix = check_path(prefix)
    binary = check_path(prefix / 'bin/binit')
    receipt_path = check_path(prefix / 'share/binit/install.json')
    receipt = load_receipt(receipt_path, binary)
    old_binary = regular(binary) if receipt else None
    old_rc_path = receipt.get('rc_file') if receipt else None
    if rc_path is not None:
        rc_path = check_path(rc_path)
        if old_rc_path and str(rc_path) != old_rc_path:
            raise ValueError('installation already owns a different startup file')
    elif old_rc_path:
        rc_path = check_path(old_rc_path)
    if rc_path and rc_path in (binary, receipt_path, source):
        raise ValueError('startup file must differ from the executable and receipt')
    block = decode_block(receipt) if receipt else b''
    rc = rc_update(rc_path, block, startup(binary)) if rc_path else None
    if receipt and digest(source_data) == receipt['sha256'] and (not rc or rc[1] == rc[3]):
        print(startup(binary))
        return
    updated_receipt = {'schema': 1, 'binary': str(binary), 'sha256': digest(source_data), 'mode': 0o755,
                       'rc_file': str(rc_path) if rc_path else None,
                       'rc_block': base64.b64encode(rc[4]).decode() if rc else None}
    if old_binary and old_binary[0] != source_data:
        backup(binary, *old_binary)
    if rc and rc[0] and rc[1] != rc[3]:
        backup(rc_path, rc[1], rc[2])
    binary_written = rc_written = False
    try:
        atomic(binary, source_data, 0o755)
        binary_written = True
        if rc and rc[1] != rc[3]:
            atomic(rc_path, rc[3], rc[2])
            rc_written = True
        atomic(receipt_path, (json.dumps(updated_receipt, indent=2) + '\n').encode(), 0o600)
    except BaseException:
        if rc_written:
            if rc[0]: atomic(rc_path, rc[1], rc[2])
            else: rc_path.unlink(missing_ok=True)
        if binary_written:
            if old_binary: atomic(binary, *old_binary)
            else: binary.unlink(missing_ok=True)
        raise
    print(startup(binary))


def uninstall(prefix):
    prefix = check_path(prefix)
    binary = check_path(prefix / 'bin/binit')
    receipt_path = check_path(prefix / 'share/binit/install.json')
    receipt = load_receipt(receipt_path, binary)
    if not receipt:
        raise ValueError('no installation receipt')
    rc_path = check_path(receipt['rc_file']) if receipt.get('rc_file') else None
    block = decode_block(receipt)
    rc = None
    if rc_path:
        original, mode = regular(rc_path)
        if not block or original.count(block) != 1:
            raise ValueError(f'installed startup block changed: {rc_path}')
        before, after = original.split(block, 1)
        # Keep the separator if the user appended content after an installation
        # made from a file that originally lacked a trailing newline.
        separator = b'\n' if block.startswith(b'\n') and after else b''
        rc = (original, mode, before + separator + after)
    previous_binary = regular(binary)
    rc_written = False
    try:
        if rc:
            atomic(rc_path, rc[2], rc[1])
            rc_written = True
        binary.unlink()
        receipt_path.unlink()
    except BaseException:
        if not binary.exists(): atomic(binary, *previous_binary)
        if rc_written: atomic(rc_path, rc[0], rc[1])
        raise
    print('Binit uninstalled')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=ROOT / 'target/release/binit')
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local')
    parser.add_argument('--rc-file', type=Path)
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    if args.uninstall and args.rc_file:
        parser.error('--uninstall uses the startup file in the receipt')
    try:
        if args.uninstall:
            uninstall(args.prefix)
        else:
            install(args.binary, args.prefix, args.rc_file)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f'binit install: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
