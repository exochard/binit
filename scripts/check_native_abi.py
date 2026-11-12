#!/usr/bin/env python3
"""Require rejection of a native module built for a different Bash ABI."""
import argparse
import os
from pathlib import Path
import subprocess


def check(module, bash):
    env = dict(os.environ, HISTFILE='/dev/null', INPUTRC='/dev/null', TERM='dumb')
    env.pop('BASH_ENV', None)
    env.pop('ENV', None)
    result = subprocess.run([str(bash), '--noprofile', '--norc', '-ic',
                             'if enable -f "$1" binit_complete; then exit 91; fi; '
                             'type -t binit_complete && exit 92; printf "rejected-safely\\n"',
                             'abi-check', str(module)], env=env, capture_output=True, text=True, timeout=10)
    if result.returncode != 0 or 'rejected-safely' not in result.stdout or 'native module requires Bash' not in result.stderr:
        raise AssertionError(f'ABI rejection failed: {result.returncode}\n{result.stdout}\n{result.stderr}')
    print('Native ABI mismatch: rejected before callback registration')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', type=Path)
    parser.add_argument('bash', type=Path)
    args = parser.parse_args()
    check(args.module.resolve(), args.bash.resolve())
