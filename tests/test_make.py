import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MakeTargets(unittest.TestCase):
    def test_targets_run_directly_without_inherited_rust_wrapper(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-make-', dir=ROOT) as directory:
            binaries = Path(directory)
            for name in ('cargo', 'bash', 'python3'):
                program = binaries / name
                program.write_text('#!/usr/bin/python3\nimport json,os,sys\n'
                                   'print(json.dumps({"program":os.path.basename(sys.argv[0]),"args":sys.argv[1:],"wrapper":os.environ.get("RUSTC_WRAPPER")}))\n')
                program.chmod(0o700)
            for name in ('systemd-run', 'free', 'nvidia-smi', 'nice'):
                program = binaries / name
                program.write_text('#!/bin/sh\nprintf "unexpected host wrapper or probe\\n" >&2\nexit 99\n')
                program.chmod(0o700)
            environment = dict(os.environ, PATH=str(binaries) + ':' + os.environ['PATH'],
                               RUSTC_WRAPPER='/broken/sccache')
            for target, program, wrapper in [('build', 'cargo', ''), ('verify', 'bash', ''), ('bench', 'python3', ''), ('build', 'cargo', '/chosen/cache')]:
                with self.subTest(target=target):
                    arguments = ['make', '--no-print-directory', '-f', str(ROOT / 'Makefile'), target]
                    if wrapper:
                        arguments.append('RUSTC_WRAPPER=' + wrapper)
                    result = subprocess.run(arguments,
                                            cwd=ROOT, env=environment, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
                    self.assertEqual(len(rows), 1, result.stdout)
                    self.assertEqual(rows[0]['program'], program)
                    self.assertEqual(rows[0]['wrapper'], wrapper)

