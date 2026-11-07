"""Antigravity init model evidence participates in normal verification."""
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

MODEL = 'gemini-3.8-flash-high'
OTHER_MODEL = 'gemini-4-argon-high'


def init(model=MODEL):
    return {'event': 'init', 'conversation_id': 'session-id', 'init': {'model': model}}


def completed(**fields):
    return {'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'Review complete', **fields}}


def stream(events):
    return '\n'.join(json.dumps(event) for event in events)


class TestAntigravityInitModel(unittest.TestCase):
    def invoke(self, root, events):
        route = common.normalize_model_route('antigravity', 'google', MODEL)
        with patch.object(run, 'run_process_with_timeout',
                          return_value=(0, stream(events), '', False, True)):
            return run.invoke_antigravity(
                binary='unused', route=route, prompt_text='review', attempt_dir=root,
                effort=None, keep_session=True, resume_session_id=None,
                sandbox_mode='read-only', cwd=str(root), timeout_sec=30, child_env={},
                recipe_flags={})

    def test_init_model_without_terminal_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self.invoke(root, [init(), completed()])
            self.assertEqual(result['status'], 'ok', result)
            self.assertEqual(result['observed_model'], MODEL)
            self.assertEqual(result['session_id'], 'session-id')
            self.assertEqual((root / 'result.md').read_text(), 'Review complete\n')

    def test_init_substitution_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self.invoke(root, [init(OTHER_MODEL), completed()])
            self.assertEqual(result['error_class'], 'model_unavailable', result)
            self.assertFalse((root / 'result.md').exists())

    def test_later_terminal_or_top_level_model_change_is_rejected(self):
        for later in (completed(model=OTHER_MODEL),
                      {'event': 'step', 'model': OTHER_MODEL}):
            with self.subTest(later=later), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                result = self.invoke(root, [init(), later, completed()])
                self.assertEqual(result['error_class'], 'model_unavailable', result)
                self.assertIn('model changed', result['error'])
                self.assertFalse((root / 'result.md').exists())

    def test_consistent_model_evidence_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), [init(), completed(model=MODEL)])
        self.assertEqual(result['status'], 'ok', result)

    def test_missing_or_invalid_init_model_keeps_existing_behavior(self):
        for malformed in ({}, None, [], {'model': None}, {'model': ''}, {'model': 42}):
            with self.subTest(init=malformed), tempfile.TemporaryDirectory() as td:
                result = self.invoke(Path(td), [{'event': 'init', 'init': malformed}, completed()])
                self.assertEqual(result['status'], 'ok', result)
                self.assertIsNone(result['observed_model'])

    def test_unrelated_nested_model_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), [
                {'event': 'step_update', 'init': {'model': OTHER_MODEL}}, completed()])
        self.assertEqual(result['status'], 'ok', result)
        self.assertIsNone(result['observed_model'])

    def test_init_does_not_replace_terminal_completion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self.invoke(root, [init()])
            self.assertEqual(result['error_class'], 'format', result)
            self.assertFalse((root / 'result.md').exists())

    def test_task_metadata_verifies_init_model(self):
        route = common.normalize_model_route('antigravity', 'google', MODEL)
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {'CROSS_SUBAGENTS_CACHE_DIR': str(Path(td) / 'cache')}
        ), patch.object(run, 'resolve_explicit_route', return_value=route), patch.object(
            run, 'probe_all', return_value={'agents': {'antigravity': {
                'usable': True, 'binary': 'unused', 'version': '1.4.12',
                'recipe_flags': {'has_mode': True, 'has_sandbox': True}}}}
        ), patch.object(run, 'run_process_with_timeout',
                        return_value=(0, stream([init(), completed()]), '', False, True)):
            result = run.execute_task(
                run_id='init-model', task_id='review', route_str=route['route_id'],
                prompt_text='review', cwd=td, sandbox_mode='read-only',
                force_route=True, no_fallback=True)
            self.assertEqual(result['status'], 'ok', result)
            meta = json.loads((Path(td) / 'cache/runs/init-model/review/meta.json').read_text())
            self.assertEqual(meta['observed_model'], MODEL)
            self.assertTrue(meta['model_verified'])


if __name__ == '__main__':
    unittest.main()
