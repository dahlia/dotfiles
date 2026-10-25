#!/usr/bin/env python3
"""Offline regression tests for the v2 gate and native export classifier."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).with_name('opencode-review.sh')
MODEL = 'deepseek/deepseek-flash'
spec = importlib.util.spec_from_file_location('opencode_private', RUNNER.with_name('opencode-private.py'))
private = importlib.util.module_from_spec(spec)
spec.loader.exec_module(private)


class OpenCodeReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='test-opencode-v2-')
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.root / 'calls'
        self.env.update(PATH=f'{self.bin}:{os.environ["PATH"]}', CALLS=str(self.calls))

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, *args):
        return subprocess.run(['bash', str(RUNNER), *map(str, args)],
                              env=self.env, text=True, capture_output=True)

    def fake_version(self, version):
        fake = self.bin / 'opencode'
        fake.write_text('#!/bin/bash\n'
                        'printf "%s\\n" "$*" >> "$CALLS"\n'
                        'if [ "$1" = --version ]; then printf "%s\\n" "$OC_VERSION"; exit; fi\n'
                        'if [ "$1" = serve ]; then echo \'{"url":"http://127.0.0.1:12345"}\'; cat >/dev/null; exit; fi\n'
                        'if [ "$1" = api ]; then echo \'{"data":[]}\'; exit; fi\n'
                        'if [ "$1" = models ]; then echo deepseek/deepseek-flash; exit; fi\n'
                        'exit 90\n')
        fake.chmod(0o755)
        self.env['OC_VERSION'] = version

    def test_old_and_unknown_versions_never_discover_or_run(self):
        for version in ['1.18.31', 'opencode v1.18.31', 'dev', '', 'v2']:
            with self.subTest(version=version):
                self.fake_version(version)
                for args, code in [(('models',), 3), (('run', MODEL, '1'), 30)]:
                    self.calls.unlink(missing_ok=True)
                    result = self.invoke(*args)
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertIn('WARNING:', result.stderr)
                    self.assertIn('upgrade', result.stderr.lower())
                    self.assertEqual(self.calls.read_text(), '--version\n')

    def test_v2_model_discovery_uses_private_server(self):
        for version in ['2.1.3', 'opencode v2.1.3', 'v2.0.0', 'opencode v3.0.0']:
            with self.subTest(version=version):
                self.fake_version(version)
                result = self.invoke('models')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), MODEL)
                self.assertIn('models --server http://127.0.0.1:12345', self.calls.read_text())
                self.assertIn('model.list', self.calls.read_text())

    def test_private_runner_preserves_embedded_command_separator(self):
        self.fake_version('2.1.3')
        fake = self.bin / 'opencode'
        fake.write_text(fake.read_text().replace(
            'if [ "$1" = --version ]',
            'if [ "$1" = -- ]; then shift; fi\nif [ "$1" = --version ]'))
        config = self.root / 'config.json'
        config.write_text('{}')
        result = subprocess.run([sys.executable, str(RUNNER.with_name('opencode-private.py')),
            '--directory', str(self.root), '--config', str(config),
            '--diagnostics', str(self.root / 'private'),
            '--cli-json', json.dumps([str(fake), '--']),
            '--', 'models', '--server', '@SERVER@'], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(MODEL, result.stdout)
        self.assertIn('-- models --server http://127.0.0.1:12345', self.calls.read_text())

    def test_preflight_accepts_defaults_and_extra_denies_but_rejects_grants(self):
        deny = {'action': '*', 'resource': '*', 'effect': 'deny'}
        allow = dict(deny, effect='allow')
        extra = dict(deny, action='browser')
        config = {'agents': {'review': {'permissions': [deny]}}}
        for rules, valid in [([deny], True), ([allow, deny], True),
                             ([allow, deny, extra], True), ([deny, allow], False),
                             ([allow], False)]:
            response = {'data': [{'id': 'review', 'mode': 'primary', 'permissions': rules}]}
            if valid:
                private.validate_agent(response, config, 'review')
            else:
                with self.assertRaises(ValueError):
                    private.validate_agent(response, config, 'review')

    def test_guarded_run_with_v2_server_preflight_and_export(self):
        fake = self.bin / 'opencode'
        fake.write_text("""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as f:
    f.write(' '.join(args) + '\\n')
if args == ['--version']:
    print('opencode v2.1.3')
elif args[:2] == ['debug', 'paths']:
    print('data  /tmp/opencode-test-data')
elif args[0] == 'serve':
    print(json.dumps({'url': 'http://127.0.0.1:12345'}), flush=True)
    sys.stdin.read()
elif args[0] == 'api':
    if 'agent.list' in args:
        config = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
        agent = config['agents']['review-loop-flash']
        print(json.dumps({'data': [dict(agent, id='review-loop-flash')]}))
    else:
        print('{"data": []}')
elif args[0] == 'run':
    assert os.environ['GIT_OPTIONAL_LOCKS'] == '0'
    assert '--server' in args and '--variant' not in args and '--pure' not in args
    assert args[args.index('--model') + 1] == 'deepseek/deepseek-flash#high'
    assert sys.stdin.read().strip() == 'Review this fixture.'
    print(json.dumps({'type': 'step_finish', 'sessionID': 'ses_test', 'part': {'reason': 'stop'}}))
elif args[:2] == ['session', 'export']:
    print(os.environ['OC_EXPORT'])
else:
    sys.exit(90)
""")
        fake.chmod(0o755)
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', 'commit',
                        '--allow-empty', '-qm', 'Fixture'], check=True)
        work = self.root / 'work'
        work.mkdir()
        (work / 'opencode-prompt.txt').write_text('Review this fixture.')
        self.env.update(REPO_ROOT=str(repo), WORK=str(work),
                        OC_EXPORT=json.dumps({'data': self.fixture()}))
        result = self.invoke('run', MODEL, '1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('STATUS=clean', result.stdout)
        self.assertIn('session export --standalone ses_test', self.calls.read_text())

    def fixture(self):
        return {'info': {'id': 'ses_test'}, 'messages': [
            {'type': 'user', 'text': 'Review'},
            {'type': 'assistant', 'agent': 'review-loop-flash',
             'model': {'providerID': 'deepseek', 'id': 'deepseek-flash', 'variant': 'high'},
             'time': {'created': 1, 'completed': 2}, 'finish': 'stop',
             'content': [{'type': 'text', 'text': 'NO ACTIONABLE FINDINGS'}]}]}

    def classify(self, data, events=None, exit_code=0):
        prefix = self.root / 'attempt'
        Path(f'{prefix}.export.json').write_text(json.dumps(data))
        Path(f'{prefix}.jsonl').write_text(events if events is not None else
            json.dumps({'type': 'step_finish', 'sessionID': 'ses_test',
                        'part': {'reason': 'stop'}}) + '\n')
        Path(f'{prefix}.exit').write_text(str(exit_code))
        Path(f'{prefix}.err').write_text('')
        return self.invoke('check', prefix, MODEL)

    def test_native_v2_clean_and_findings(self):
        data = self.fixture()
        self.assertEqual(self.classify(data).returncode, 0)
        data['messages'][-1]['content'][0]['text'] = 'A concrete defect.'
        self.assertEqual(self.classify(data).returncode, 10)

    def test_wrapped_v2_export(self):
        self.assertEqual(self.classify({"data": self.fixture()}).returncode, 0)

    def test_model_variant_agent_and_incomplete_rejected(self):
        for field, value in [('model', {'providerID': 'other', 'id': 'deepseek-flash', 'variant': 'high'}),
                             ('model', {'providerID': 'deepseek', 'id': 'deepseek-flash'}),
                             ('agent', 'build'), ('finish', 'length'), ('time', {'created': 1})]:
            with self.subTest(field=field, value=value):
                data = self.fixture()
                data['messages'][-1][field] = value
                self.assertEqual(self.classify(data).returncode, 20)

    def test_assistant_error_is_not_clean(self):
        data = self.fixture()
        data['messages'][-1]['error'] = {'type': 'api', 'message': 'Unauthorized', 'status': 401}
        result = self.classify(data)
        self.assertEqual(result.returncode, 30, result.stdout)
        self.assertIn('assistant error:', result.stdout)

    def test_native_tool_denial_is_not_clean(self):
        data = self.fixture()
        data['messages'][-1]['content'].append({
            'type': 'tool', 'name': 'read', 'state': {'status': 'error',
            'input': {'path': '.env'}, 'error': {'type': 'permission', 'message': 'denied'}}})
        result = self.classify(data)
        self.assertEqual(result.returncode, 21, result.stdout)
        self.assertIn('DENIED=read:', result.stdout)

    def test_only_latest_round_is_classified(self):
        data = self.fixture()
        old = copy.deepcopy(data['messages'][-1])
        old['model']['providerID'] = 'old'
        data['messages'][0:0] = [{'type': 'user', 'text': 'Old'}, old]
        self.assertEqual(self.classify(data).returncode, 0)
        data['messages'].append({'type': 'user', 'text': 'New unanswered prompt'})
        self.assertEqual(self.classify(data).returncode, 20)

    def test_empty_truncated_failed_or_invalid_stream_is_not_clean(self):
        data = self.fixture()
        data['messages'][-1]['content'] = []
        self.assertEqual(self.classify(data).returncode, 31)
        self.assertEqual(self.classify(self.fixture(), events='bad json').returncode, 20)
        self.assertEqual(self.classify(self.fixture(), exit_code=1).returncode, 20)


if __name__ == '__main__':
    unittest.main()
