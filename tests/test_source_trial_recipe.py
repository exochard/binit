"""A rejected source bundle cannot execute the documented bootstrap verifier."""
import hashlib
import io
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from package_sources import package


class SourceTrialRecipeTests(unittest.TestCase):
    def test_bootstrap_uses_trusted_manifest_and_stops_before_rejected_code(self):
        scratch = ROOT / 'tmp'
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='trial-recipe-test-', dir=scratch)
        self.addCleanup(temporary.cleanup)
        work = Path(temporary.name)
        source = work / 'source'
        (source / 'scripts').mkdir(parents=True)
        for name, data in {'Cargo.toml': b'[package]\nname="fixture"\n', 'Cargo.lock': b'# fixture\n',
                           'LICENSE': b'MIT\n', 'scripts/demo.bash': b':\n',
                           'scripts/unpack_sources.py': (ROOT / 'scripts/unpack_sources.py').read_bytes()}.items():
            (source / name).write_bytes(data)
        for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture']):
            subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True)
        blocks = re.findall(r'```bash\n(.*?)\n```', (ROOT / 'docs/source-trial.md').read_text(), re.S)
        block = next(text for text in blocks if 'Expected sources.json SHA256' in text)
        for variant in ('valid', 'wrong-manifest', 'altered-archive-and-checksum-list', 'planted-module'):
            with self.subTest(variant=variant):
                bundle = work / variant
                manifest = package(source, [], bundle)
                trusted_sha = hashlib.sha256((bundle / 'sources.json').read_bytes()).hexdigest()
                if variant == 'planted-module':
                    # A module beside the received files must not shadow the standard library.
                    (bundle / 'json.py').write_text('from pathlib import Path\nPath("REJECTED_CODE_EXECUTED").touch()\n'
                                                    'raise SystemExit(1)\n')
                if variant == 'wrong-manifest':
                    trusted_sha = '0' * 64
                if variant == 'altered-archive-and-checksum-list':
                    archive = bundle / manifest['core']['artifact']
                    payload = b'from pathlib import Path\nPath("REJECTED_CODE_EXECUTED").touch()\n'
                    with tarfile.open(archive, 'w:gz') as tar:
                        member = tarfile.TarInfo('scripts/unpack_sources.py')
                        member.size, member.mode = len(payload), 0o644
                        tar.addfile(member, io.BytesIO(payload))
                    (bundle / 'SHA256SUMS').write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n')
                result = subprocess.run(['bash', '-c', block.replace('/path/to/source-bundle', shlex.quote(str(bundle)))],
                                        input=trusted_sha + '\n', text=True, capture_output=True, timeout=10)
                self.assertFalse((bundle / 'REJECTED_CODE_EXECUTED').exists())
                if variant in ('valid', 'planted-module'):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue((bundle / 'trial/core/Cargo.lock').is_file())
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((bundle / 'bootstrap').exists())
