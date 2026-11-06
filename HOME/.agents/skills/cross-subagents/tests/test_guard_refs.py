"""Shared ref changes must not invalidate a stable checkout review."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import guard


class TestGuardRefs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        (self.root / 'file').write_text('one\n')
        self.git('add', 'file')
        self.git('commit', '-m', 'initial')
        self.initial = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args, root=None):
        return subprocess.check_output(
            ['git', '-C', str(root or self.root), *args],
            env={**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull,
                 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_OPTIONAL_LOCKS': '0'},
            stderr=subprocess.PIPE, text=True)

    def check(self, action, expected):
        before = guard.snapshot(self.root)
        self.assertEqual(before['status'], 'ok')
        action()
        report = guard.verify(self.root, before)
        self.assertEqual(report['status'], expected, report)

    def test_unrelated_worktree_commit_and_pack(self):
        other = Path(self.temp.name) / 'other'
        self.git('worktree', 'add', '-b', 'other', str(other))
        def change():
            self.git('commit', '--allow-empty', '-m', 'other', root=other)
            self.git('pack-refs', '--all', '--prune')
        self.check(change, 'clean')

    def test_current_ref_storage_pack_is_clean(self):
        self.check(lambda: self.git('pack-refs', '--all', '--prune'), 'clean')

    def test_packed_current_ref_move_detected(self):
        self.git('commit', '--allow-empty', '-m', 'second')
        second = self.git('rev-parse', 'HEAD').strip()
        self.git('update-ref', 'refs/heads/main', self.initial)
        self.git('pack-refs', '--all', '--prune')
        def change():
            self.git('update-ref', 'refs/heads/main', second)
            self.git('pack-refs', '--all', '--prune')
        self.check(change, 'mutated')

    def test_detached_head_unrelated_pack_is_clean(self):
        self.git('checkout', '--detach')
        self.check(lambda: self.git('pack-refs', '--all', '--prune'), 'clean')

    def test_unborn_head_becomes_branch_detected(self):
        self.git('symbolic-ref', 'HEAD', 'refs/heads/unborn')
        self.check(lambda: self.git('update-ref', 'refs/heads/unborn', self.initial), 'mutated')


if __name__ == '__main__':
    unittest.main()
