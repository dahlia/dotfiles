"""Interrupted Claude sessions retain recoverable evidence without claiming completion."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import run

MODEL = 'claude-fable-5-1'


class TestClaudeTimeout(unittest.TestCase):
    def invoke(self, root, events, *, timed_out=True):
        stdout = '\n'.join(json.dumps(event) for event in events)
        with patch.object(run, 'run_process_with_timeout',
                          return_value=(124 if timed_out else 0, stdout, '', timed_out, True)):
            return run.invoke_claude_code(
                binary='unused', route={'model_id': MODEL, 'canonical_key': MODEL},
                prompt_text='review', attempt_dir=root, effort=None, keep_session=True,
                resume_session_id=None, sandbox_mode='workspace-write', cwd=None,
                timeout_sec=30, child_env={}, recipe_flags={})

    def test_init_session_and_model_survive_timeout_with_unknown_usage(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': 'recover-me', 'model': MODEL},
            {'type': 'assistant', 'message': {'model': MODEL, 'content': [{'type': 'tool_use'}]}},
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self.invoke(root, events)
            self.assertEqual(result['status'], 'timeout', result)
            self.assertEqual(result['session_id'], 'recover-me')
            self.assertEqual(result['observed_model'], MODEL)
            self.assertFalse(result['model_verified'])
            self.assertEqual(result['usage'], {'input_tokens': None, 'output_tokens': None})
            self.assertFalse(result['usage_complete'])
            self.assertFalse(result['session_cleaned_up'])
            self.assertFalse((root / 'result.md').exists())
            self.assertEqual(run._find_json_objects((root / 'claude_output.json').read_text()), events)

    def test_terminal_usage_zero_is_known_and_terminal_model_verifies(self):
        events = [{'type': 'result', 'subtype': 'success', 'result': 'Review complete',
                   'session_id': 'recover-me', 'usage': {'input_tokens': 0, 'output_tokens': 0},
                   'modelUsage': {MODEL: {}}}]
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), events, timed_out=False)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['usage'], {'input_tokens': 0, 'output_tokens': 0})
        self.assertTrue(result['usage_complete'])
        self.assertTrue(result['model_verified'])

    def test_notification_cannot_verify_incomplete_prompt(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': 'recover-me', 'model': MODEL},
            {'type': 'result', 'subtype': 'success', 'result': 'Background done',
             'origin': {'kind': 'task-notification'},
             'usage': {'input_tokens': 2, 'output_tokens': 3}, 'modelUsage': {MODEL: {}}},
        ]
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), events)
        self.assertEqual(result['status'], 'timeout', result)
        self.assertEqual(result['session_id'], 'recover-me')
        self.assertFalse(result['model_verified'])
        self.assertFalse(result['usage_complete'])
        self.assertEqual(result['usage'], {'input_tokens': 2, 'output_tokens': 3})

    def test_terminal_model_takes_precedence_over_init_model(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': 'recover-me', 'model': MODEL},
            {'type': 'result', 'subtype': 'success', 'result': 'Wrong model review',
             'modelUsage': {'claude-sonnet-5': {}}},
        ]
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), events, timed_out=False)
        self.assertEqual(result['error_class'], 'model_unavailable', result)
        self.assertEqual(result['observed_model'], 'claude-sonnet-5')
        self.assertFalse(result['model_verified'])


if __name__ == '__main__':
    unittest.main()
