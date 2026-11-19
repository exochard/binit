from pathlib import Path
import json
import subprocess
import sys
import tarfile
import termios
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bench_ecosystem as corpus


class EcosystemIntegrityTests(unittest.TestCase):
    def test_source_content_and_executable_mode_are_bound_to_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            tree = base / 'sources/example'
            tree.mkdir(parents=True)
            path = tree / 'loader.sh'
            path.write_bytes(b':\n')
            path.chmod(0o755)
            archive = base / 'example.tar.gz'
            with tarfile.open(archive, 'w:gz') as bundle:
                bundle.add(path, arcname='upstream-revision/loader.sh')
            spec = {'sources': {'example': {'archive_sha256': corpus.digest(archive)}}}
            self.assertEqual(corpus.verify_sources(base, spec)['example']['verified_files'], 1)
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'source differs'):
                corpus.verify_sources(base, spec)
            path.chmod(0o755)
            path.write_bytes(b'false\n')
            with self.assertRaisesRegex(RuntimeError, 'source differs'):
                corpus.verify_sources(base, spec)
            path.write_bytes(b':\n')
            (tree / 'extra.sh').write_bytes(b'sleep .1\n')
            with self.assertRaisesRegex(RuntimeError, 'inventory differs'):
                corpus.verify_sources(base, spec)

    def test_source_symlink_target_and_archive_hash_are_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            tree = base / 'sources/example'
            tree.mkdir(parents=True)
            link = tree / 'link'
            link.symlink_to('target')
            archive = base / 'example.tar.gz'
            with tarfile.open(archive, 'w:gz') as bundle:
                member = tarfile.TarInfo('upstream/link')
                member.type = tarfile.SYMTYPE
                member.linkname = 'target'
                bundle.addfile(member)
            spec = {'sources': {'example': {'archive_sha256': corpus.digest(archive)}}}
            corpus.verify_sources(base, spec)
            link.unlink()
            link.symlink_to('other')
            with self.assertRaisesRegex(RuntimeError, 'source differs'):
                corpus.verify_sources(base, spec)
            archive.write_bytes(b'changed archive')
            with self.assertRaisesRegex(RuntimeError, 'archive hash differs'):
                corpus.verify_sources(base, spec)

    def test_effect_probe_accepts_real_effects_and_rejects_missing_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            body = corpus.FIXTURE.read_text().replace('@@ID@@', 'p00')
            probe = corpus.effect_check(1, target)
            for shell in ('/bin/bash', '/bin/zsh'):
                for broken, expected in ((False, 0), (True, 23)):
                    code = body + ('\nunalias corpus_alias_p00\n' if broken else '\n')
                    setup = 'shopt -s expand_aliases\n' if shell == '/bin/bash' else ''
                    process = subprocess.run([shell, '-c', setup + code + probe + 'corpus_verify'],
                                             env=corpus.environment(target), capture_output=True)
                    self.assertEqual(process.returncode, expected, (shell, process.stderr))

    def test_effect_probe_rejects_unexported_false_alias_and_later_wrong_function(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            body = ''.join(corpus.FIXTURE.read_text().replace('@@ID@@', f'p{i:02d}') for i in range(2))
            probe = corpus.effect_check(2, target)
            for shell in ('/bin/bash', '/bin/zsh'):
                cases = [(body, 0), (body.replace('export CORPUS_EXPORT_', 'CORPUS_EXPORT_'), 24),
                         (body + "alias corpus_alias_p01='false'\n", 23),
                         (body + 'corpus_fn_p01() { return 99; }\n', 25)]
                for code, expected in cases:
                    setup = 'shopt -s expand_aliases\n' if shell == '/bin/bash' else ''
                    process = subprocess.run([shell, '-c', setup + code + probe + 'corpus_verify'],
                                             env=corpus.environment(target), capture_output=True)
                    self.assertEqual(process.returncode, expected, (shell, process.stderr))

    def test_prompt_before_editor_readiness_is_not_usable_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            rc = work / 'rc'
            # Emit a real-looking prompt while still in cooked mode, as Zsh can.
            rc.write_text('printf "BENCH> "\nsleep .1\nEDITOR_READY=yes\nPS1="BENCH> "\n')
            shell = corpus.BoundedShell('/bin/bash', rc, work, corpus.environment(work))
            try:
                corpus.wait_for_editor(shell)
                self.assertFalse(termios.tcgetattr(shell.fd)[3] & termios.ICANON)
                shell.settled(b':')
                shell.command('')
                offset = len(shell.raw)
                shell.command('printf "\\036READY:%s\\036" "$EDITOR_READY"')
                self.assertIn(b'\x1eREADY:yes\x1e', shell.raw[offset:])
            finally:
                shell.close()

    def test_changed_executable_stops_next_pair_and_retains_partial_receipt(self):
        with tempfile.TemporaryDirectory(dir=corpus.ROOT / 'tmp') as temporary:
            base = Path(temporary)
            binary = base / 'binary'
            binary.write_bytes(b'original')
            output = base / 'receipt.json'
            argv = ['bench_ecosystem.py', '--sources', str(base), '--binary', str(binary),
                    '--work', str(base / 'work'), '--output', str(output),
                    '--subjects', 'bash', '--counts', '0', '--pairs', '2', '--runs', '1']
            def drift(*_):
                binary.write_bytes(b'changed')
                return {'effect_status': 0}
            with patch.object(sys, 'argv', argv), patch.object(corpus, 'verify_sources', return_value={}), \
                    patch.object(corpus, 'observe', side_effect=drift):
                with self.assertRaisesRegex(RuntimeError, 'inputs changed before pair'):
                    corpus.main()
            receipt = json.loads(output.read_text())
            self.assertFalse(receipt['complete_two_run_framework_corpus'])
            self.assertEqual(len(receipt['runs'][0]), 1)
            self.assertIn('inputs changed', receipt['error'])

    def test_preparation_failure_has_receipt_and_case(self):
        with tempfile.TemporaryDirectory(dir=corpus.ROOT / 'tmp') as temporary:
            base = Path(temporary)
            binary = base / 'binary'
            binary.write_bytes(b'not executed')
            output = base / 'receipt.json'
            argv = ['bench_ecosystem.py', '--sources', str(base), '--binary', str(binary),
                    '--work', str(base / 'work'), '--output', str(output),
                    '--subjects', 'binit', '--counts', '0']
            with patch.object(sys, 'argv', argv), patch.object(corpus, 'verify_sources', return_value={}), \
                    patch.object(corpus, 'configuration', side_effect=RuntimeError('prepare failed')):
                with self.assertRaisesRegex(RuntimeError, 'prepare failed'):
                    corpus.main()
            receipt = json.loads(output.read_text())
            self.assertFalse(receipt['complete_two_run_framework_corpus'])
            self.assertEqual(receipt['active_case'], 'binit-0')
            self.assertIn('prepare failed', receipt['error'])

    def test_resource_sampler_retains_owned_zombie_without_rss(self):
        process = subprocess.Popen(['/bin/true'])
        try:
            deadline = time.monotonic() + 2
            while '\nState:\tZ' not in (Path('/proc') / str(process.pid) / 'status').read_text():
                if time.monotonic() > deadline:
                    self.fail('owned child did not exit')
                time.sleep(.001)
            with patch.object(Path, 'iterdir', side_effect=PermissionError('fixture denies fd access')):
                sample = corpus.resource_sample(process.pid)
            self.assertEqual(sample['processes'], 1)
            self.assertEqual(sample['zombies'], 1)
            self.assertEqual(sample['rss_kib_sum'], 0)
            self.assertEqual(sample['fds_sum'], 0)
            self.assertEqual(sample['samples'][0]['state'], 'Z')
        finally:
            process.wait()

    def test_resource_sampler_retains_vanished_child_and_rejects_malformed_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            for pid in (123, 456):
                root = proc / str(pid)
                (root / 'fd').mkdir(parents=True)
                (root / f'task/{pid}').mkdir(parents=True)
                (root / 'status').write_text('Name: fixture\nState:\tR\n')
            (proc / '123/statm').write_text('5 2 0 0 0 0 0\n')
            (proc / '123/task/123/children').write_text('456\n')
            sample = corpus.resource_sample(123, proc)
            self.assertEqual(sample['processes'], 1)
            self.assertEqual(sample['discovered_processes'], 2)
            self.assertEqual(sample['vanished_processes'], 1)
            self.assertTrue(sample['partial'])
            self.assertEqual(sample['samples'][0]['resident_pages'], 2)
            (proc / '456/statm').write_text('malformed\n')
            with self.assertRaisesRegex(RuntimeError, 'malformed statm'):
                corpus.resource_sample(123, proc)

    def test_resource_sampler_accepts_reported_zero_without_inferring_process_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            root = proc / '123'
            (root / 'fd').mkdir(parents=True)
            (root / 'task/123').mkdir(parents=True)
            (root / 'status').write_text('Name: fixture\nState:\tR\n')
            (root / 'statm').write_text('0 0 0 0 0 0 0\n')
            (root / 'task/123/children').write_text('')
            sample = corpus.resource_sample(123, proc)
            self.assertFalse(sample['partial'])
            self.assertEqual(sample['rss_kib_sum'], 0)
            self.assertEqual(sample['samples'][0]['state'], 'R')
            with patch.object(Path, 'iterdir', side_effect=PermissionError('fixture denies fd access')):
                with self.assertRaises(PermissionError):
                    corpus.resource_sample(123, proc)

    def test_resource_sampler_rereads_a_child_denied_during_exit(self):
        # Seen on a 6.18 kernel: a child first read as live releases its memory and denies its fd
        # listing while its state still reads R; some reach Z before the second read.
        live = 'Name: fixture\nState:\tS (sleeping)\nThreads:\t1\n'
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            for pid in (123, 456):
                root = proc / str(pid)
                (root / 'fd').mkdir(parents=True)
                (root / f'task/{pid}').mkdir(parents=True)
                (root / f'task/{pid}/children').write_text('')
            (proc / '123/task/123/children').write_text('456\n')
            listed = Path.iterdir

            def sample(denied, after_status, after_statm):
                for pid in (123, 456):
                    (proc / f'{pid}/status').write_text(live)
                    (proc / f'{pid}/statm').write_text('5 2 0 0 0 0 0\n')

                def deny(path):
                    if path.parent.name == str(denied) and path.name == 'fd':
                        (path.parent / 'status').write_text(after_status)
                        (path.parent / 'statm').write_text(after_statm)
                        raise PermissionError('fixture denies fd access')
                    return listed(path)

                with patch.object(Path, 'iterdir', deny):
                    return corpus.resource_sample(123, proc)

            exiting = 'Name: fixture\nState:\tR (running)\nThreads:\t1\n'
            result = sample(456, exiting, '0 0 0 0 0 0 0\n')
            self.assertEqual((result['processes'], result['vanished_processes'], result['partial']), (1, 1, True))
            result = sample(456, 'Name: fixture\nState:\tZ (zombie)\nThreads:\t1\n', '0 0 0 0 0 0 0\n')
            zombie = result['samples'][-1]
            self.assertEqual((result['processes'], result['zombies'], result['partial']), (2, 1, False))
            self.assertEqual((zombie['state'], zombie['resident_pages'], zombie['rss_kib'], zombie['fds']), ('Z', 0, 0, 0))
            with self.assertRaises(PermissionError):
                sample(456, exiting, '5 2 0 0 0 0 0\n')
            with self.assertRaises(PermissionError):
                sample(456, 'Name: fixture\nState:\tR (running)\nThreads:\t2\n', '0 0 0 0 0 0 0\n')
            with self.assertRaisesRegex(RuntimeError, 'owned shell disappeared'):
                sample(123, exiting, '0 0 0 0 0 0 0\n')


if __name__ == '__main__':
    unittest.main()
