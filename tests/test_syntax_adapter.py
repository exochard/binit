import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('fetch_syntax', ROOT / 'scripts/fetch_syntax.py')
fetch_syntax = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch_syntax)


class GrammarAcquisitionTests(unittest.TestCase):
    def test_fetch_and_verify_rejects_tampering(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-syntax-', dir=ROOT) as directory:
            root = Path(directory)
            manifest = root / 'manifest.json'
            content = b'grammar source'
            manifest.write_text(json.dumps({'repository': 'example/repo', 'commit': 'a' * 40,
                                            'files': {'src/parser.c': hashlib.sha256(content).hexdigest()}}))
            destination = root / 'source'
            (destination / 'src').mkdir(parents=True)
            (destination / 'src/parser.c').write_bytes(content)
            (destination / 'grammar.js').write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            with patch.object(fetch_syntax, 'MANIFEST', manifest), patch.object(fetch_syntax, 'GRAMMAR_FILES', {}), patch.object(fetch_syntax, 'PATCHED_GRAMMAR_SHA256', digest), patch.object(fetch_syntax, 'PATCHED_PARSER_SHA256', digest):
                fetch_syntax.verify(destination)
                (destination / 'src/parser.c').write_bytes(b'tampered')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    fetch_syntax.verify(destination)

    def test_fetch_rejects_bad_download(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-syntax-', dir=ROOT) as directory:
            root = Path(directory)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'repository': 'example/repo', 'commit': 'a' * 40,
                                            'files': {'src/parser.c': '0' * 64}}))
            with patch.object(fetch_syntax, 'MANIFEST', manifest), patch.object(fetch_syntax, 'GRAMMAR_FILES', {}), patch.object(fetch_syntax.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad')):
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    fetch_syntax.fetch(root / 'source')


if __name__ == '__main__':
    unittest.main()
