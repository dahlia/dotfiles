"""OpenCode must retain failed-attempt usage and read its project shell spools."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import common
import run


class TestOpenCodeUsageSpools(unittest.TestCase):
    def invoke(self, root, events, *, timed_out=True, policy=None, env=None):
        route = common.normalize_model_route('opencode', 'deepseek', 'deepseek-flash')
        attempt = root / 'attempt'
        attempt.mkdir()
        with patch.object(run, 'run_process_with_timeout', return_value=(
                124 if timed_out else 0, '\n'.join(map(json.dumps, events)), '', timed_out, True)) as proc:
            result = run.invoke_opencode(
                'opencode', route, 'review', attempt, None, True, None,
                'read-only', str(root), 10, env or dict(os.environ),
                recipe_flags={'has_session_cmd': False}, policy=policy or {})
            return result, proc.call_args

    def test_timeout_retains_completed_steps_and_deduplicates_replays(self):
        step = {'type': 'step_finish', 'part': {
            'id': 'p1', 'reason': 'tool-calls', 'cost': .02,
            'tokens': {'input': 12, 'output': 3, 'reasoning': 2,
                       'cache': {'read': 4, 'write': 1}}}}
        events = [step, step, {'type': 'step_finish', 'part': {
            'id': 'p2', 'reason': 'tool-calls', 'cost': .03,
            'tokens': {'input': 7, 'output': 5}}},
            {'type': 'step_start'}, {'type': 'text', 'part': {'text': 'unfinished'}}]
        with tempfile.TemporaryDirectory() as td:
            result, _ = self.invoke(Path(td), events)
            self.assertEqual(result['status'], 'timeout')
            self.assertEqual(result['error_class'], 'timeout')
            self.assertEqual(result['usage'], {
                'input_tokens': 19, 'output_tokens': 8, 'reasoning_tokens': 2,
                'cache_read_tokens': 4, 'cache_write_tokens': 1, 'cost': .05})
            self.assertFalse((Path(td) / 'attempt/result.md').exists())

    def test_failed_no_completion_retains_usage(self):
        events = [{'type': 'step_finish', 'part': {
            'reason': 'tool-calls', 'tokens': {'input': 21, 'output': 9}}}]
        with tempfile.TemporaryDirectory() as td:
            result, _ = self.invoke(Path(td), events, timed_out=False)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['usage']['input_tokens'], 21)

    def test_shell_spool_grant_uses_git_project_and_child_data_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            project = 'a' * 40
            (root / '.git/opencode').write_text(project)
            env = {**os.environ, 'XDG_DATA_HOME': str(root / 'data')}
            result, call = self.invoke(root, [], policy={'allow_shell': True}, env=env)
            cfg = json.loads(call.kwargs['env']['OPENCODE_CONFIG_CONTENT'])
            expected = {'action': 'external_directory', 'resource':
                        str(root / 'data/opencode/shell' / project) + '/*', 'effect': 'allow'}
            self.assertIn(expected, cfg['agents']['review-shell']['permissions'])
            for action in ('edit', 'write', 'patch'):
                self.assertIn({'action': action, 'resource': '*', 'effect': 'deny'}, cfg['permissions'])
            self.assertNotIn({'action': 'external_directory', 'resource':
                              str(root / 'data/opencode') + '/*', 'effect': 'allow'}, cfg['permissions'])

    def test_worktree_spool_uses_common_git_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.org', 'commit', '--allow-empty', '-qm', 'initial'], check=True)
            project = 'b' * 40
            (repo / '.git/opencode').write_text(project)
            wt = root / 'worktree'
            subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', str(wt)], check=True)
            env = {**os.environ, 'XDG_DATA_HOME': str(root / 'data')}
            self.assertEqual(run._opencode_shell_output_dirs(str(wt), env),
                             [str(root / 'data/opencode/shell' / project)])

    def test_shell_spool_falls_back_to_root_commit_without_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.org', 'commit', '--allow-empty', '-qm', 'initial'], check=True)
            project = subprocess.check_output(
                ['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
            env = {**os.environ, 'XDG_DATA_HOME': str(root / 'data')}
            self.assertEqual(run._opencode_shell_output_dirs(td, env),
                             [str(root / 'data/opencode/shell' / project)])

    def test_resume_grants_original_project_spool_only_for_verified_metadata(self):
        original, current = 'b' * 40, 'a' * 40
        for info, permitted, export_cleanup in [
            ({'id': 'session-test', 'projectID': original}, True, True),
            ({'id': 'session-test', 'projectID': original}, False, False),
            ({'id': 'another-session', 'projectID': original}, False, True),
            ({'id': 'session-test', 'projectID': '../source'}, False, True),
            ({'id': 'session-test', 'projectID': original + '/*'}, False, True),
        ]:
            with self.subTest(info=info), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                subprocess.run(['git', 'init', '-q', str(root)], check=True)
                (root / '.git/opencode').write_text(current)
                env = {**os.environ, 'XDG_DATA_HOME': str(root / 'data')}
                calls = []
                def process(cmd, *args, **kwargs):
                    calls.append((cmd, kwargs))
                    if cmd[1:3] == ['session', 'export']:
                        return 0, json.dumps({'info': info}), '', False, export_cleanup
                    return 124, '', '', True, True
                route = common.normalize_model_route('opencode', 'deepseek', 'deepseek-flash')
                with patch.object(run, 'run_process_with_timeout', side_effect=process):
                    result = run.invoke_opencode(
                        'opencode', route, 'review', root, None, True, 'session-test',
                        'read-only', td, 10, env, recipe_flags={}, policy={'allow_shell': True})
                cfg = json.loads(calls[-1][1]['env']['OPENCODE_CONFIG_CONTENT'])
                rules = cfg['agents']['review-shell']['permissions']
                original_rule = {'action': 'external_directory', 'resource':
                                 str(root / 'data/opencode/shell' / original) + '/*', 'effect': 'allow'}
                self.assertEqual(original_rule in rules, permitted)
                self.assertIn({'action': 'external_directory', 'resource':
                               str(root / 'data/opencode/shell' / current) + '/*', 'effect': 'allow'}, rules)
                self.assertEqual(result['status'], 'timeout')
                self.assertEqual(result['cleanup_verified'], export_cleanup)
                self.assertTrue(all(r['resource'].startswith(str(root / 'data/opencode/shell'))
                                    for r in rules if r['action'] == 'external_directory' and r['effect'] == 'allow'))

    def test_no_shell_does_not_grant_spool(self):
        with tempfile.TemporaryDirectory() as td, patch.object(run, '_opencode_shell_output_dirs') as dirs:
            self.invoke(Path(td), [])
            dirs.assert_not_called()

    def test_report_emits_colon_trailers(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'CROSS_SUBAGENTS_CACHE_DIR': td}):
            run.record_native_task('trailers', 'review', 'Codex', 'gpt-6.1-sol')
            report = run.build_run_report('trailers')
            self.assertEqual(report['assisted_by_trailers'], ['Assisted-by: Codex:gpt-6.1-sol'])
            self.assertIn('Assisted-by: Codex:gpt-6.1-sol', run.format_human_run_report(report))


if __name__ == '__main__':
    unittest.main()
