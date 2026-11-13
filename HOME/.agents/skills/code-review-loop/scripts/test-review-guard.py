#!/usr/bin/env python3
"""Exercise recovery artifacts and review result checks without model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.root / 'source').write_bytes(b'original\x00bytes')
        (self.root / '.gitignore').write_text('ignored\ncache/\n')
        (self.root / 'ignored').write_text('private ignored input')
        (self.root / 'untracked').write_text('untracked input')
        (self.root / 'link').symlink_to('source')
        self.git('add', 'source', '.gitignore', 'link')
        self.git('commit', '-qm', 'initial')
        self.prompt = self.base / 'prompt'
        self.prompt.write_text('review')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args])

    def run_guard(self, code, allowed=None):
        self.prefix = self.base / 'attempt'
        result = subprocess.run([sys.executable, str(SCRIPTS / 'review-guard.py'),
            '--root', str(self.root), '--prefix', str(self.prefix), '--stdin', str(self.prompt),
            '--', sys.executable, '-c', code], capture_output=True,
            env={**os.environ, 'REVIEW_OUTPUT_PATHS': json.dumps(allowed or [])})
        return result.returncode

    def begin_native(self):
        self.prefix = self.base / 'native-attempt'
        result = subprocess.run([sys.executable, str(SCRIPTS / 'review-guard.py'),
            '--root', str(self.root), '--prefix', str(self.prefix), '--stdin', str(self.prompt),
            '--native-begin', '--agent', 'Codex', '--model', 'test-model'], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        handoff = json.loads(result.stdout)
        self.result_file = self.base / 'native-result.md'
        self.result_file.write_text('NO ACTIONABLE FINDINGS')
        return {'handoff_id': handoff['handoff_id'], 'status': 'completed',
                'completion_verified': True, 'host_task_id': 'host-agent-1',
                'completion_event': 'host final-answer event 1', 'model_verified': False,
                'observed_model': None, 'denials_observed': [],
                'result_file': str(self.result_file)}

    def finish_native(self, receipt):
        receipt_path = self.base / 'receipt.json'
        receipt_path.write_text(json.dumps(receipt))
        return subprocess.run([sys.executable, str(SCRIPTS / 'review-guard.py'),
            '--root', str(self.root), '--prefix', str(self.prefix),
            '--native-finish', str(receipt_path)], capture_output=True)

    def test_native_handoff_has_real_completion_and_no_cli_cleanup(self):
        receipt = self.begin_native()
        self.assertFalse(Path(str(self.prefix) + '.guard.json').exists())
        self.assertTrue(Path(str(self.prefix) + '.backup.tar').is_file())
        result = self.finish_native(receipt)
        self.assertEqual(result.returncode, 0, result.stderr)
        meta = json.loads(Path(str(self.prefix) + '.stdout').read_text())
        self.assertTrue(meta['completion_verified'])
        self.assertIsNone(meta['cleanup_verified'])
        self.assertFalse(meta['model_verified'])
        self.assertEqual(meta['host_task_id'], 'host-agent-1')
        self.assertEqual(json.loads(Path(str(self.prefix) + '.guard.json').read_text())['status'], 'unchanged')
        self.assertEqual(self.finish_native(receipt).returncode, 2)

    def assert_recovery_data(self, retained):
        for suffix in ('.backup.tar', '.before.json', '.after.json'):
            self.assertEqual(Path(str(self.prefix) + suffix).exists(), retained, suffix)
        for suffix in ('.stdout', '.exit', '.guard.json'):
            self.assertTrue(Path(str(self.prefix) + suffix).is_file(), suffix)

    def test_completed_native_attempt_releases_recovery_data(self):
        receipt = self.begin_native()
        self.assertEqual(self.finish_native(receipt).returncode, 0)
        self.assert_recovery_data(False)
        self.assertTrue(self.result_file.is_file())
        self.assertTrue(Path(str(self.prefix) + '.receipt.json').is_file())

    def test_unverified_native_attempt_retains_recovery_data(self):
        receipt = self.begin_native()
        receipt['completion_verified'] = False
        self.assertEqual(self.finish_native(receipt).returncode, 1)
        self.assert_recovery_data(True)

    def test_finished_cli_attempt_releases_data_even_after_quota_failure(self):
        for status, code in [('ok', 0), ('quota', 1), ('timeout', 124)]:
            with self.subTest(status=status):
                self.base = self.base / status
                self.base.mkdir()
                metadata = {'status': status, 'cleanup_verified': True}
                command = f'print({json.dumps(metadata)!r}); raise SystemExit({code})'
                self.assertEqual(self.run_guard(command), code)
                self.assert_recovery_data(False)
                self.assertTrue(Path(str(self.prefix) + '.stderr').is_file())

    def test_cli_cleanup_unknown_or_incomplete_retains_recovery_data(self):
        for cleanup in [None, False, 'true', 1]:
            with self.subTest(cleanup=cleanup):
                self.base = self.base / str(cleanup)
                self.base.mkdir()
                metadata = {'status': 'ok', 'cleanup_verified': cleanup}
                self.assertEqual(self.run_guard(f'print({json.dumps(metadata)!r})'), 0)
                self.assert_recovery_data(True)

    def test_mutated_cli_attempt_retains_data_despite_verified_cleanup(self):
        code = ("from pathlib import Path; Path('source').write_text('changed'); "
                f'print({json.dumps({"status": "ok", "cleanup_verified": True})!r})')
        self.assertEqual(self.run_guard(code), 40)
        self.assert_recovery_data(True)

    def test_non_json_cli_output_retains_recovery_data(self):
        self.assertEqual(self.run_guard("print('ordinary output')"), 0)
        self.assert_recovery_data(True)

    def test_legacy_cleanup_previews_then_deletes_only_redundant_files(self):
        self.assertEqual(self.run_guard('pass'), 0)
        Path(str(self.prefix) + '.stdout').write_text(
            json.dumps({'status': 'failed', 'cleanup_verified': True}))
        command = [sys.executable, str(SCRIPTS / 'review_artifacts.py'), str(self.prefix)]
        preview = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertIn(str(self.prefix) + '.backup.tar', preview.stdout)
        self.assert_recovery_data(True)
        subprocess.run(command + ['--apply'], capture_output=True, check=True)
        self.assert_recovery_data(False)
        subprocess.run(command + ['--apply'], capture_output=True, check=True)

    def test_legacy_cleanup_rejects_missing_terminal_marker(self):
        self.assertEqual(self.run_guard('pass'), 0)
        Path(str(self.prefix) + '.stdout').write_text(
            json.dumps({'status': 'ok', 'cleanup_verified': True}))
        Path(str(self.prefix) + '.exit').unlink()
        subprocess.run([sys.executable, str(SCRIPTS / 'review_artifacts.py'),
                        str(self.prefix), '--apply'], capture_output=True, check=True)
        self.assertTrue(Path(str(self.prefix) + '.backup.tar').is_file())

    def test_native_handoff_detects_writes_between_host_turns(self):
        receipt = self.begin_native()
        (self.root / 'source').write_bytes(b'reviewer write')
        self.assertEqual(self.finish_native(receipt).returncode, 40)
        self.assertIn('tree:source', self.changes())

    def test_native_handoff_rejects_stale_completion_receipt(self):
        receipt = self.begin_native()
        receipt['handoff_id'] = 'another-attempt'
        self.assertEqual(self.finish_native(receipt).returncode, 1)

    def test_native_handoff_rejects_unfinished_host(self):
        receipt = self.begin_native()
        receipt['completion_verified'] = False
        self.assertEqual(self.finish_native(receipt).returncode, 1)

    def test_native_handoff_requires_terminal_event(self):
        receipt = self.begin_native()
        del receipt['completion_event']
        self.assertEqual(self.finish_native(receipt).returncode, 1)

    def test_native_handoff_requires_permission_inspection(self):
        receipt = self.begin_native()
        del receipt['denials_observed']
        self.assertEqual(self.finish_native(receipt).returncode, 1)

    def test_native_handoff_cannot_verify_absent_model(self):
        receipt = self.begin_native()
        receipt['model_verified'] = True
        self.assertEqual(self.finish_native(receipt).returncode, 1)

    def test_native_handoff_keeps_begin_exclusions(self):
        receipt = self.begin_native()
        (self.root / 'cache').mkdir()
        (self.root / 'cache' / 'output').write_text('not originally excluded')
        receipt_path = self.base / 'receipt.json'
        receipt_path.write_text(json.dumps(receipt))
        result = subprocess.run([sys.executable, str(SCRIPTS / 'review-guard.py'),
            '--root', str(self.root), '--prefix', str(self.prefix),
            '--native-finish', str(receipt_path)], capture_output=True,
            env={**os.environ, 'REVIEW_OUTPUT_PATHS': '["cache"]'})
        self.assertEqual(result.returncode, 40)
        self.assertIn('tree:cache/output', self.changes())

    def test_native_handoff_rejects_empty_review(self):
        receipt = self.begin_native()
        self.result_file.write_text('   ')
        self.assertEqual(self.finish_native(receipt).returncode, 1)

    def test_native_handoff_rejects_model_substitution(self):
        receipt = self.begin_native()
        receipt.update(observed_model='different-model', model_verified=True)
        self.assertEqual(self.finish_native(receipt).returncode, 1)

    def test_native_handoff_keeps_permission_denials_for_classification(self):
        receipt = self.begin_native()
        receipt['denials_observed'] = ['read denied']
        self.assertEqual(self.finish_native(receipt).returncode, 0)
        self.assertEqual(json.loads(Path(str(self.prefix) + '.stdout').read_text())['denials_observed'], ['read denied'])

    def test_binary_ignored_untracked_symlink_and_mode_backup(self):
        self.assertEqual(self.run_guard("from pathlib import Path; Path('source').write_bytes(b'changed'); Path('ignored').unlink(); Path('untracked').chmod(0o700); Path('link').unlink(); Path('link').symlink_to('ignored')"), 40)
        with tarfile.open(str(self.prefix) + '.backup.tar') as archive:
            self.assertEqual(archive.extractfile('tree/source').read(), b'original\x00bytes')
            self.assertEqual(archive.extractfile('tree/ignored').read(), b'private ignored input')
            self.assertEqual(archive.getmember('tree/link').linkname, 'source')
            self.assertIn('git/index', archive.getnames())
        changes = json.loads(Path(str(self.prefix) + '.guard.json').read_text())['changes']
        self.assertEqual(set(changes), {'tree:source', 'tree:ignored', 'tree:untracked', 'tree:link'})

    def test_index_only_change_is_detected(self):
        (self.root / 'source').write_bytes(b'dirty')
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','add','source'],check=True)"), 40)
        self.assertIn('git:index', json.loads(Path(str(self.prefix) + '.guard.json').read_text())['changes'])

    def test_read_only_git_inspection_does_not_mutate(self):
        (self.root / 'source').write_bytes(b'dirty worktree')
        code = (
            "import os, subprocess; "
            "env = dict(os.environ); env.pop('GIT_OPTIONAL_LOCKS', None); "
            "subprocess.run(['git', 'status', '--short'], check=True, env=env); "
            "subprocess.run(['git', 'diff'], check=True, env=env)"
        )
        self.assertEqual(self.run_guard(code), 0)

    def test_nested_tracked_file_inside_allowed_output_is_protected(self):
        sub = self.root / 'sub'
        sub.mkdir()

        def git_sub(*args):
            return subprocess.check_output(['git', '-C', str(sub), *args])

        git_sub('init', '-q')
        git_sub('config', 'user.email', 'test@example.invalid')
        git_sub('config', 'user.name', 'Test')
        (sub / 'build').mkdir()
        (sub / 'build' / 'file').write_text('original')
        git_sub('add', 'build/file')
        git_sub('commit', '-qm', 'nested')
        code = "from pathlib import Path; Path('sub/build/file').write_text('changed')"
        self.assertEqual(self.run_guard(code, allowed=['sub/build']), 40)

    def test_linked_worktree_commondir_change_is_detected(self):
        wt = self.base / 'wt'
        self.git('worktree', 'add', '-q', '-b', 'wt', str(wt))
        raw = subprocess.check_output(
            ['git', '-C', str(wt), 'rev-parse', '--git-path', 'commondir']
        ).decode().strip()
        commondir = Path(raw)
        if not commondir.is_absolute():
            commondir = wt / commondir
        code = (
            "from pathlib import Path; "
            f"Path({str(commondir)!r}).write_text('elsewhere\\n')"
        )
        prefix = self.base / 'wt-attempt'
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / 'review-guard.py'),
                '--root', str(wt),
                '--prefix', str(prefix),
                '--stdin', str(self.prompt),
                '--',
                sys.executable, '-c', code,
            ],
            capture_output=True,
            env={**os.environ, 'REVIEW_OUTPUT_PATHS': '[]'},
        )
        # Either the changed pointer is detected (40) or resolution fails and
        # the guard refuses to report anything (2). Both are fail-closed.
        self.assertIn(result.returncode, (40, 2), result.stderr)

    def test_intent_to_add_transition_is_detected(self):
        (self.root / 'empty').write_bytes(b'')
        self.git('add', '-N', 'empty')
        code = "import subprocess; subprocess.run(['git', 'add', 'empty'], check=True)"
        self.assertEqual(self.run_guard(code), 40)

    def test_split_index_dependencies_are_backed_up(self):
        self.git('update-index', '--split-index')
        self.assertEqual(self.run_guard('pass'), 0)
        with tarfile.open(str(self.prefix) + '.backup.tar') as archive:
            self.assertTrue(any(name.startswith('git/sharedindex.') for name in archive.getnames()))

    def test_unrelated_ref_creation_is_allowed(self):
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','branch','unexpected'],check=True)"), 0)

    def test_other_worktree_branch_rename_config_is_allowed(self):
        other = self.base / 'other'
        self.git('worktree', 'add', '-b', 'other', str(other))
        self.git('config', 'branch.other.remote', 'origin')
        code = (
            "import subprocess; "
            f"subprocess.run(['git','-C',{str(other)!r},'branch','-m','renamed'],check=True)"
        )
        self.assertEqual(self.run_guard(code), 0)

    def test_current_branch_config_change_is_detected(self):
        branch = self.git('symbolic-ref', '--short', 'HEAD').decode().strip()
        code = (
            "import subprocess; "
            f"subprocess.run(['git','config',{'branch.' + branch + '.remote'!r},'changed'],check=True)"
        )
        self.assertEqual(self.run_guard(code), 40)

    def test_nonbranch_config_change_is_detected(self):
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','config','core.filemode','false'],check=True)"), 40)

    def test_current_branch_object_change_is_detected(self):
        self.git('commit', '--allow-empty', '-qm', 'second')
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','update-ref','HEAD','HEAD~1'],check=True)"), 40)
        self.assertIn('git:head-object', self.changes())

    def test_current_branch_switch_at_same_commit_is_detected(self):
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','checkout','-qb','other'],check=True)"), 40)
        self.assertIn('git:HEAD', self.changes())

    def test_other_worktree_commit_is_allowed(self):
        other = self.base / 'other'
        self.git('worktree', 'add', '-q', '-b', 'other', str(other))
        code = ("import subprocess; subprocess.run(['git','-C'," + repr(str(other)) +
                ",'commit','--allow-empty','-qm','other commit'],check=True)")
        self.assertEqual(self.run_guard(code), 0)

    def test_linked_checkout_ignores_main_worktree_commit(self):
        original = self.root
        linked = self.base / 'linked'
        self.git('worktree', 'add', '-q', '-b', 'linked', str(linked))
        self.root = linked
        code = ("import subprocess; subprocess.run(['git','-C'," + repr(str(original)) +
                ",'commit','--allow-empty','-qm','main commit'],check=True); "
                "subprocess.run(['git','pack-refs','--all','--prune'],check=True)")
        self.assertEqual(self.run_guard(code), 0)

    def test_symbolic_head_chain_change_is_detected(self):
        branch = self.git('symbolic-ref', 'HEAD').decode().strip()
        self.git('symbolic-ref', 'refs/heads/alias', branch)
        self.git('symbolic-ref', 'HEAD', 'refs/heads/alias')
        self.git('branch', 'other')
        code = "import subprocess; subprocess.run(['git','symbolic-ref','refs/heads/alias','refs/heads/other'],check=True)"
        self.assertEqual(self.run_guard(code), 40)
        self.assertIn('git:head-refs', self.changes())
        self.assertNotIn('git:head-object', self.changes())

    def test_ref_packing_is_allowed(self):
        self.git('branch', 'other')
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','pack-refs','--all','--prune'],check=True)"), 0)

    def test_unrelated_packed_ref_update_and_delete_are_allowed(self):
        self.git('branch', 'other')
        self.git('pack-refs', '--all', '--prune')
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','branch','-D','other'],check=True); subprocess.run(['git','pack-refs','--all','--prune'],check=True)"), 0)

    def test_packed_current_branch_change_is_detected(self):
        self.git('commit', '--allow-empty', '-qm', 'second')
        self.git('pack-refs', '--all', '--prune')
        code = "import subprocess; subprocess.run(['git','update-ref','HEAD','HEAD~1'],check=True); subprocess.run(['git','pack-refs','--all','--prune'],check=True)"
        self.assertEqual(self.run_guard(code), 40)
        self.assertIn('git:head-object', self.changes())

    def test_detached_head_change_is_detected(self):
        self.git('commit', '--allow-empty', '-qm', 'second')
        self.git('checkout', '-q', '--detach')
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','update-ref','HEAD','HEAD~1'],check=True)"), 40)

    def test_unborn_branch_is_allowed(self):
        self.git('checkout', '-q', '--orphan', 'unborn')
        self.assertEqual(self.run_guard('pass'), 0)

    def test_allowed_output_and_nonzero_exit(self):
        self.assertEqual(self.run_guard("from pathlib import Path; Path('cache').mkdir(); Path('cache/output').write_text('ok'); print('test failed'); raise SystemExit(7)", ['cache']), 7)
        self.assertEqual(Path(str(self.prefix) + '.exit').read_text(), '7\n')
        self.assertIn('test failed', Path(str(self.prefix) + '.stdout').read_text())

    def test_tracked_file_inside_output_is_protected(self):
        self.git('add', '-f', 'ignored')
        self.assertEqual(self.run_guard("from pathlib import Path; Path('ignored').write_text('changed')", ['ignored']), 40)

    def test_output_traversal_rejected(self):
        self.assertEqual(self.run_guard('pass', ['../outside']), 2)
        self.assertFalse(Path(str(self.prefix) + '.stdout').exists())

    def test_new_ignored_file_is_detected(self):
        self.assertEqual(self.run_guard("from pathlib import Path; Path('cache').mkdir(); Path('cache/new').write_text('unexpected')"), 40)

    def changes(self):
        return json.loads(Path(str(self.prefix) + '.guard.json').read_text())['changes']

    def add_submodule(self):
        upstream = self.base / 'upstream'
        upstream.mkdir()
        for args in (('init', '-q'), ('config', 'user.email', 'test@example.invalid'),
                     ('config', 'user.name', 'Test')):
            subprocess.check_output(['git', '-C', str(upstream), *args])
        (upstream / 'file').write_text('submodule file')
        subprocess.check_output(['git', '-C', str(upstream), 'add', 'file'])
        subprocess.check_output(['git', '-C', str(upstream), 'commit', '-qm', 'initial'])
        self.git('-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(upstream), 'sub')
        self.git('commit', '-qm', 'add submodule')

    def test_orphan_gitlink_is_allowed(self):
        # A gitlink with no .gitmodules entry and no repository behind it has
        # no Git state of its own; the index already protects its commit.
        head = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('update-index', '--add', '--cacheinfo', f'160000,{head},orphan')
        (self.root / 'orphan').mkdir()
        self.assertEqual(self.run_guard('pass'), 0)

    def test_uninitialized_submodule_is_allowed(self):
        self.add_submodule()
        self.git('submodule', '--quiet', 'deinit', '-f', 'sub')
        self.assertEqual(self.run_guard('pass'), 0)

    def test_submodule_is_backed_up_and_unchanged_run_passes(self):
        self.add_submodule()
        self.assertEqual(self.run_guard('pass'), 0)
        with tarfile.open(str(self.prefix) + '.backup.tar') as archive:
            names = archive.getnames()
        self.assertIn('tree/sub/file', names)
        self.assertIn('nested/sub/HEAD', names)
        self.assertIn('nested/sub/index', names)
        self.assertFalse(any('/objects/' in name for name in names))

    def test_submodule_commit_is_detected(self):
        self.add_submodule()
        code = ("import subprocess; from pathlib import Path; Path('sub/file').write_text('x'); "
                "subprocess.run(['git','-C','sub','-c','user.email=a@b.invalid','-c','user.name=A',"
                "'commit','-qam','sneaky'],check=True); "
                "subprocess.run(['git','-C','sub','checkout','-q','HEAD~1','--','file'],check=True)")
        self.assertEqual(self.run_guard(code), 40)
        # The working tree is back to its original bytes; only the
        # submodule's own Git state reveals the commit.
        self.assertNotIn('tree:sub/file', self.changes())
        self.assertTrue(any(change.startswith('git[sub]:') for change in self.changes()))

    def test_submodule_branch_switch_is_detected(self):
        self.add_submodule()
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','-C','sub','checkout','-qb','other'],check=True)"), 40)
        self.assertIn('git[sub]:HEAD', self.changes())

    def test_submodule_working_tree_change_is_detected(self):
        self.add_submodule()
        self.assertEqual(self.run_guard("from pathlib import Path; Path('sub/file').write_text('changed')"), 40)
        self.assertIn('tree:sub/file', self.changes())

    def test_nested_repository_metadata_without_objects(self):
        nested = self.root / 'nested'
        nested.mkdir()
        for args in (('init', '-q'), ('config', 'user.email', 'test@example.invalid'),
                     ('config', 'user.name', 'Test')):
            subprocess.check_output(['git', '-C', str(nested), *args])
        (nested / 'file').write_text('nested file')
        subprocess.check_output(['git', '-C', str(nested), 'add', 'file'])
        subprocess.check_output(['git', '-C', str(nested), 'commit', '-qm', 'initial'])
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','-C','nested','checkout','-qb','unexpected'],check=True)"), 40)
        self.assertTrue(any(change.startswith('git[nested]:') for change in self.changes()))
        with tarfile.open(str(self.prefix) + '.backup.tar') as archive:
            names = archive.getnames()
        self.assertIn('tree/nested/file', names)
        self.assertIn('nested/nested/HEAD', names)
        self.assertFalse(any('/objects/' in name for name in names))

    def test_new_nested_repository_is_detected(self):
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','init','-q','fresh'],check=True)"), 40)

    def test_unresolvable_nested_repository_is_refused(self):
        broken = self.root / 'broken'
        broken.mkdir()
        (broken / '.git').write_text('gitdir: ../does-not-exist\n')
        self.assertEqual(self.run_guard('pass'), 2)
        self.assertFalse(Path(str(self.prefix) + '.stdout').exists())



if __name__ == '__main__':
    unittest.main()
