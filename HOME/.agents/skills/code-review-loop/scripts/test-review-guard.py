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

    def test_split_index_dependencies_are_backed_up(self):
        self.git('update-index', '--split-index')
        self.assertEqual(self.run_guard('pass'), 0)
        with tarfile.open(str(self.prefix) + '.backup.tar') as archive:
            self.assertTrue(any(name.startswith('git/sharedindex.') for name in archive.getnames()))

    def test_ref_change_is_detected(self):
        self.assertEqual(self.run_guard("import subprocess; subprocess.run(['git','branch','unexpected'],check=True)"), 40)

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


    def test_opencode_retry_keeps_evidence_and_fresh_session(self):
        work = self.base / 'work'
        work.mkdir()
        (work / 'opencode-prompt.txt').write_text('review inputs')
        mockbin = self.base / 'bin'
        mockbin.mkdir()
        mock = mockbin / 'opencode'
        mock.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
work=Path(os.environ['WORK'])
if args==['--version']:
    print('mock 1.18.34')
elif args[:2]==['debug','paths']:
    print('data  '+str(work))
elif args[:2]==['debug','config']:
    print('{}')
elif args[:2]==['debug','agent']:
    print(json.dumps({'name':'review-loop-flash','permission':[{'permission':'*','pattern':'*','action':'deny'}], 'tools':dict(edit=False,write=False,task=False,webfetch=False)}))
elif args[0]=='run':
    if os.environ.get('MOCK_MUTATE'): Path('source').write_bytes(b'reviewer edit')
    count=work/'count'
    n=int(count.read_text())+1 if count.exists() else 1
    count.write_text(str(n))
    with (work/'calls').open('a') as f: f.write(json.dumps(args)+'\\n')
    print(json.dumps({'type':'step_start','sessionID':'session'+str(n)}))
    if n==1: print(json.dumps({'type':'error','error':{'name':'APIError','data':{'statusCode':503,'message':'unavailable'}}}))
elif args[0]=='export':
    n=int((work/'count').read_text())
    info=dict(role='assistant',providerID='deepseek',modelID='deepseek-flash',variant='high',agent='review-loop-flash',finish='stop')
    if n==1: info['error']={'name':'APIError','data':{'statusCode':503,'message':'unavailable'}}
    print(json.dumps({'messages':[{'info':{'role':'user'},'parts':[]},{'info':info,'parts':[{'type':'text','text':'NO ACTIONABLE FINDINGS'}]}]}))
else:
    raise SystemExit(2)
""")
        mock.chmod(0o700)
        result = subprocess.run(['bash',str(SCRIPTS/'opencode-review.sh'),'run','deepseek/deepseek-flash','2','previous-session'], capture_output=True, env={**os.environ, 'REPO_ROOT':str(self.root),'WORK':str(work),'PATH':str(mockbin)+os.pathsep+os.environ['PATH'],'REVIEW_OUTPUT_PATHS':'[]'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b'STATUS=transient', result.stdout)
        self.assertIn(b'STATUS=clean', result.stdout)
        calls=[json.loads(line) for line in (work/'calls').read_text().splitlines()]
        self.assertIn('--session', calls[0])
        self.assertNotIn('--session', calls[1])
        self.assertEqual(len(list(work.glob('opencode-*/a*.backup.tar'))),2)
        (work/'count').write_text('0')
        mutation = subprocess.run(['bash',str(SCRIPTS/'opencode-review.sh'),'run','deepseek/deepseek-flash','1'], capture_output=True, env={**os.environ,'MOCK_MUTATE':'1','REPO_ROOT':str(self.root),'WORK':str(work),'PATH':str(mockbin)+os.pathsep+os.environ['PATH'],'REVIEW_OUTPUT_PATHS':'[]'})
        self.assertEqual(mutation.returncode,40,mutation.stdout+mutation.stderr)
        self.assertIn(b'STATUS=mutated',mutation.stdout)
        self.assertEqual((work/'count').read_text(),'1')



class OpenCodeResults(unittest.TestCase):
    def check(self, finish='stop', text='NO ACTIONABLE FINDINGS', error=None, variant='high', denied=False):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / 'round'
            prefix.with_suffix('.exit').write_text('0')
            prefix.with_suffix('.err').write_text('')
            prefix.with_suffix('.jsonl').write_text(json.dumps({'sessionID':'test'}) + '\n')
            info = dict(role='assistant', providerID='deepseek', modelID='deepseek-flash', variant=variant, agent='review-loop-flash', finish=finish)
            if error:
                info['error'] = error
            parts = [dict(type='text', text=text)]
            if denied:
                parts.append(dict(type='tool', tool='bash', state=dict(status='error', error='permission denied', input=dict(command='git diff'))))
            prefix.with_suffix('.export.json').write_text(json.dumps({'messages':[{'info':{'role':'user'},'parts':[]}, {'info':info,'parts':parts}]}))
            result = subprocess.run(['bash', str(SCRIPTS/'opencode-review.sh'), 'check', str(prefix), 'deepseek/deepseek-flash'], capture_output=True)
            return result.returncode

    def test_clean_and_findings(self):
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.check(text='P1: concrete defect'), 10)

    def test_empty_reply_and_transient_provider(self):
        self.assertEqual(self.check(text=''), 31)
        self.assertEqual(self.check(error={'name':'APIError','data':{'statusCode':503,'message':'unavailable'}}), 31)

    def test_auth_model_mismatch_and_denials(self):
        self.assertEqual(self.check(error={'name':'APIError','data':{'statusCode':401,'message':'unauthorized'}}), 30)
        self.assertEqual(self.check(variant='low'), 20)
        self.assertEqual(self.check(denied=True), 21)
        self.assertEqual(self.check(finish='length'), 20)


if __name__ == '__main__':
    unittest.main()
