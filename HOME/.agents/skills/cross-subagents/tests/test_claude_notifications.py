"""Prompt answers survive Claude's trailing background notification results."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import run

MODEL = 'claude-sonnet-5'


def terminal(text='Review complete', **fields):
    return dict(type='result', subtype='success', result=text, is_error=False,
                session_id='resumed-session', usage={'input_tokens': 2, 'output_tokens': 3},
                modelUsage={MODEL: {}}, **fields)


class TestClaudeNotifications(unittest.TestCase):
    def invoke(self, root, events, *, exit_code=0, timed_out=False):
        stdout = 'Warning: config {deprecated}\n' + '\n'.join(json.dumps(e) for e in events)
        with patch.object(run, 'run_process_with_timeout',
                          return_value=(exit_code, stdout, '', timed_out, True)) as process:
            result = run.invoke_claude_code(
                binary='unused', route={'model_id': MODEL, 'canonical_key': MODEL},
                prompt_text='review', attempt_dir=root, effort=None, keep_session=True,
                resume_session_id='resumed-session', sandbox_mode='workspace-write',
                cwd=None, timeout_sec=30, child_env={}, recipe_flags={})
        cmd = process.call_args.args[0]
        self.assertEqual(cmd[cmd.index('--output-format') + 1], 'stream-json')
        self.assertIn('--verbose', cmd)
        self.assertIn('--resume', cmd)
        return result

    def test_prompt_result_survives_two_notifications(self):
        events = [
            {'type': 'system', 'subtype': 'init'},
            terminal(result_index=0, origin={'kind': 'prompt'}),
            terminal('First background timeout', result_index=1,
                     origin={'kind': 'task-notification'}),
            terminal('Second background timeout', result_index=2,
                     origin={'kind': 'task-notification'}),
            {'type': 'assistant', 'message': {'content': 'not a terminal answer'}},
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self.invoke(root, events)
            self.assertEqual(result['status'], 'ok', result)
            self.assertEqual((root / 'result.md').read_text(), 'Review complete\n')
            self.assertEqual(result['usage'], {'input_tokens': 6, 'output_tokens': 9})
            self.assertEqual(len(result['warnings']), 2)
            self.assertIn('Second background timeout', result['warnings'][1])
            self.assertIn('task-notification', (root / 'claude_output.json').read_text())

    def test_notification_error_is_warning_and_does_not_mask_answer(self):
        notification = terminal('quota exceeded', origin={'kind': 'task-notification'})
        notification.update(is_error=True, subtype='error_during_execution',
                            modelUsage={'claude-opus-5-5': {}},
                            permission_denials=[{'tool_name': 'Bash'}])
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), [terminal(), notification])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['observed_model'], MODEL)
        self.assertEqual(result['denials_observed'], [{'tool_name': 'Bash'}])
        self.assertIn('quota exceeded', result['warnings'][0])

    def test_notification_only_cannot_publish_an_answer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self.invoke(root, [terminal(origin={'kind': 'task-notification'})])
            self.assertEqual(result['error_class'], 'format', result)
            self.assertFalse((root / 'result.md').exists())
            self.assertIn('no prompt-response result', result['error'])

    def test_prompt_error_is_not_hidden_by_successful_notification(self):
        primary = terminal('quota exceeded')
        primary.update(is_error=True, subtype='error_during_execution')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self.invoke(root, [primary, terminal(origin={'kind': 'task-notification'})])
            self.assertEqual(result['error_class'], 'quota', result)
            self.assertFalse((root / 'result.md').exists())
            self.assertEqual(len(result['warnings']), 1)

    def test_notification_model_cannot_verify_substituted_prompt(self):
        primary = terminal()
        primary['modelUsage'] = {'claude-opus-5-5': {}}
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), [primary, terminal(origin={'kind': 'task-notification'})])
        self.assertEqual(result['error_class'], 'model_unavailable', result)

    def test_host_timeout_or_nonzero_exit_remains_failure(self):
        for kwargs in ({'exit_code': 1}, {'timed_out': True}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                result = self.invoke(root, [terminal(), terminal(origin={'kind': 'task-notification'})], **kwargs)
                self.assertNotEqual(result['status'], 'ok', result)
                self.assertFalse((root / 'result.md').exists())

    def test_legacy_single_json_without_origin(self):
        payload = terminal()
        payload.pop('type')
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), [payload])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['usage'], {'input_tokens': 2, 'output_tokens': 3})


if __name__ == '__main__':
    unittest.main()
