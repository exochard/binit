#!/usr/bin/env python3
"""Forward source-checkout installation commands to the built Binit executable."""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path,
                        default=Path(__file__).resolve().parent.parent / 'target/release/binit')
    parser.add_argument('--prefix', type=Path)
    parser.add_argument('--rc-file', type=Path)
    parser.add_argument('--uninstall', action='store_true')
    options = parser.parse_args()
    if options.uninstall and options.rc_file is not None:
        parser.error('--uninstall uses the startup file in the receipt')
    executable = str(options.binary.expanduser().absolute())
    arguments = [executable, 'uninstall' if options.uninstall else 'install']
    for flag, value in (('--prefix', options.prefix), ('--rc-file', options.rc_file)):
        if value is not None:
            arguments.extend((flag, str(value)))
    try:
        os.execv(executable, arguments)
    except OSError as error:
        print(f'binit install: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
