"""Antigravity's own deadline is a timeout, not a protocol format change."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import common
import run

NATIVE_ERROR = 'AGY_ERROR: ' + json.dumps({
    'short_error': 'print timeout after 15m0s with the turn still in progress; the response is partial',
    'status': 'DEADLINE_EXCEEDED', 'retryable': False, 'reason': 'print_timeout'})


class TestAntigravityTimeout(unittest.TestCase):
    def invoke(self, root, stdout='', stderr=NATIVE_ERROR, host_timeout=False):
        route = common.normalize_model_route('antigravity', 'google', 'gemini-4-argon-high')
        with patch.object(run, 'run_process_with_timeout',
                          return_value=(0, stdout, stderr, host_timeout, True)):
            return run.invoke_antigravity(
                binary='unused', route=route, prompt_text='review', attempt_dir=root,
                effort=None, keep_session=True, resume_session_id=None,
                sandbox_mode='read-only', cwd=str(root), timeout_sec=900, child_env={})

    def test_native_deadline_without_terminal_result(self):
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td))
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['error_class'], 'timeout')
        self.assertTrue(result['cleanup_verified'])
        self.assertIn('print_timeout', result['error'])

    def test_native_terminal_deadline_preserves_partial_result(self):
        for extra in ({'finished_reason': 'print_timeout'},
                      {'error': {'status': 'DEADLINE_EXCEEDED'}}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                stdout = json.dumps({'event': 'result', 'result': {
                    'status': 'ERROR', 'response': 'Partial review', **extra}})
                result = self.invoke(root, stdout=stdout, stderr='')
                self.assertEqual(result['status'], 'timeout')
                self.assertEqual((root / 'partial_result.md').read_text(), 'Partial review\n')
                self.assertFalse((root / 'result.md').exists())

    def test_transcript_mentions_do_not_mark_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            stdout = json.dumps({'event': 'result', 'result': {
                'status': 'SUCCESS', 'response': NATIVE_ERROR,
                'model': 'gemini-4-argon-high'}})
            result = self.invoke(Path(td), stdout=stdout, stderr='')
        self.assertEqual(result['status'], 'ok', result)

    def test_missing_result_without_deadline_remains_format(self):
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), stderr='AGY_ERROR: invalid json')
        self.assertEqual(result['error_class'], 'format')

    def test_timeout_does_not_trigger_cli_self_repair(self):
        route = common.normalize_model_route('antigravity', 'google', 'gemini-4-argon-high')
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {'CROSS_SUBAGENTS_CACHE_DIR': str(Path(td) / 'cache')}
        ), patch.object(run, 'resolve_explicit_route', return_value=route), patch.object(
            run, 'probe_all', return_value={'agents': {'antigravity': {
                'usable': True, 'binary': 'unused', 'version': '1.4.12',
                'recipe_flags': {'has_mode': True, 'has_sandbox': True}}}}
        ), patch.object(run, 'run_process_with_timeout',
                        return_value=(0, '', NATIVE_ERROR, False, True)), patch.object(
            run, '_perform_self_repair') as repair:
            result = run.execute_task(
                run_id='native-timeout', task_id='review', route_str=route['route_id'],
                prompt_text='review', cwd=td, sandbox_mode='read-only',
                force_route=True, no_fallback=True)
            self.assertEqual(result['error_class'], 'timeout', result)
            self.assertIsNone(result.get('maintenance_notice'))
            repair.assert_not_called()


if __name__ == '__main__':
    unittest.main()
