"""Antigravity's runner notice about a sandbox-blocked command is a denial."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import common
import run

BLOCKED_OUTPUT = (
    'Command exited with code 1.\n\n'
    'There were sandbox errors that may or may not be related to the failure. '
    'If you think the failure is because of running in the sandbox, you can run '
    'the command again with `BypassSandbox` set to true to request explicit user '
    'permission.\n\n'
    'Command output:\n'
    '[WARN] migrate: failed create_dir_all: ~/.local/state/mise/incomplete-installs\n'
    'mise ERROR error parsing config file: ~/.config/mise/config.toml\n'
    'mise ERROR failed create_dir_all: ~/.local/state/mise/trusted-configs\n'
    'mise ERROR Read-only file system (os error 30)\n'
)

BLOCKED_TOOL_INFO = {
    'name': 'run_command',
    'parameters': {'CommandLine': 'mise run test-shared'},
    'output': BLOCKED_OUTPUT,
}


def step_update(step_index, output=None):
    info = {'name': 'run_command', 'parameters': {'CommandLine': 'mise run test-shared'}}
    if output is not None:
        info['output'] = output
    return {
        'event': 'step_update',
        'step_update': {
            'conversation_id': 'conversation-1',
            'step_index': step_index,
            'state': 'DONE' if output is not None else 'ACTIVE',
            'step_type': 'tool',
            'tool_name': 'run_command',
            'tool_info': info,
        },
    }


def result_event(response='NO ACTIONABLE FINDINGS'):
    return {
        'event': 'result',
        'result': {
            'conversation_id': 'conversation-1',
            'status': 'SUCCESS',
            'response': response,
            'model': 'gemini-4-argon-high',
            'usage': {'input_tokens': 10, 'output_tokens': 5},
        },
    }


class TestAntigravitySandboxDenial(unittest.TestCase):
    def test_runner_notice_is_a_denial(self):
        denial = run._antigravity_sandbox_denial('run_command', BLOCKED_TOOL_INFO)
        self.assertIsNotNone(denial)
        self.assertIn('run_command', denial)
        self.assertIn('mise run test-shared', denial)
        self.assertIn('Read-only file system', denial)

    def test_command_output_mentioning_the_notice_is_not_a_denial(self):
        tool_info = {
            'name': 'run_command',
            'parameters': {'CommandLine': 'grep -r sandbox docs'},
            'output': (
                'Command exited with code 0.\n\n'
                'Command output:\n'
                'There were sandbox errors that may or may not be related to the failure.\n'
                'mise ERROR Read-only file system (os error 30)\n'
            ),
        }
        self.assertIsNone(run._antigravity_sandbox_denial('run_command', tool_info))

    def test_output_mentioning_the_notice_as_data_is_not_a_denial(self):
        # A command that dumps this skill's own tests would print the notice.
        tool_info = {
            'name': 'run_command',
            'parameters': {'CommandLine': 'sed -n 1,40p tests/test_antigravity_sandbox_denials.py'},
            'output': (
                'Command exited with code 0.\n\n'
                'Command output:\n'
                + BLOCKED_OUTPUT
            ),
        }
        self.assertIsNone(run._antigravity_sandbox_denial('run_command', tool_info))

    def test_missing_or_odd_tool_info_is_ignored(self):
        self.assertIsNone(run._antigravity_sandbox_denial('run_command', None))
        self.assertIsNone(run._antigravity_sandbox_denial('run_command', {}))
        self.assertIsNone(run._antigravity_sandbox_denial('run_command', {'output': 7}))

    def invoke(self, root, stdout):
        route = common.normalize_model_route('antigravity', 'google', 'gemini-4-argon-high')
        with patch.object(run, 'run_process_with_timeout',
                          return_value=(0, stdout, '', False, True)):
            return run.invoke_antigravity(
                binary='unused', route=route, prompt_text='review', attempt_dir=root,
                effort=None, keep_session=True, resume_session_id=None,
                sandbox_mode='read-only', cwd=str(root), timeout_sec=900, child_env={})

    def test_blocked_command_is_recorded_without_failing_the_turn(self):
        stdout = '\n'.join(json.dumps(event) for event in (
            step_update(1, BLOCKED_OUTPUT),
            step_update(2),
            result_event(),
        ))
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), stdout)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(result['denials_observed']), 1, result['denials_observed'])
        self.assertIn('mise run test-shared', result['denials_observed'][0])
        self.assertIn('Read-only file system', result['denials_observed'][0])

    def test_recovered_check_still_reports_the_denial(self):
        recovered = (
            'Command exited with code 0.\n\nCommand output:\n'
            '[test-shared] $ cargo test -p gukhanmun\n'
            'test result: ok. 14 passed; 0 failed\n'
        )
        stdout = '\n'.join(json.dumps(event) for event in (
            step_update(1, BLOCKED_OUTPUT),
            step_update(2, recovered),
            result_event(),
        ))
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), stdout)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(result['denials_observed']), 1, result['denials_observed'])

    def test_clean_stream_records_no_denial(self):
        clean = 'Command exited with code 0.\n\nCommand output:\nall green\n'
        stdout = '\n'.join(json.dumps(event) for event in (
            step_update(1, clean),
            result_event(),
        ))
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(Path(td), stdout)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['denials_observed'], [], result['denials_observed'])


if __name__ == '__main__':
    unittest.main()
