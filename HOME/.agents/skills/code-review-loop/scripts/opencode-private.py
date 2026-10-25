#!/usr/bin/env python3
"""Run one OpenCode v2 command on an owned, authenticated loopback server.

Warm plugins before validating the agent; v2.1.3 agent.list races activation.
No shell commands, model requests, or global service changes during preflight.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import selectors
import subprocess
import sys


def validate_agent(response, config, agent):
    expected = config['agents'][agent]['permissions']
    matches = [a for a in response['data'] if a['id'] == agent]
    if len(matches) != 1 or matches[0].get('mode') != 'primary':
        raise ValueError('dedicated primary agent did not resolve')
    # V2 prepends builtin defaults. Our catchall deny and ordered exceptions
    # must be the final rules, so inherited defaults cannot override them.
    if not expected or expected[0] != {'action': '*', 'resource': '*', 'effect': 'deny'}:
        raise ValueError('configured permissions do not start with default deny')
    actual = matches[0].get('permissions', [])
    positions = [i for i in range(len(actual) - len(expected) + 1)
                 if actual[i:i + len(expected)] == expected]
    if not positions or any(rule.get('effect') != 'deny'
                            for rule in actual[positions[-1] + len(expected):]):
        raise ValueError('resolved agent permissions differ or grant later access')


def main():
    split = sys.argv.index('--')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--agent')
    parser.add_argument('--diagnostics', required=True)
    parser.add_argument('--cli-json', default='["opencode"]')
    args = parser.parse_args(sys.argv[1:split])
    args.cli = json.loads(args.cli_json)
    if not isinstance(args.cli, list) or not args.cli or not all(isinstance(x, str) and x for x in args.cli):
        parser.error('--cli-json must be a nonempty JSON string array')
    command = sys.argv[split + 1:]
    if not command:
        parser.error('a command is required after --')
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text())
    env = dict(os.environ, OPENCODE_CONFIG_CONTENT=json.dumps(config),
               OPENCODE_PASSWORD=secrets.token_urlsafe(32),
               OPENCODE_CONFIG='', OPENCODE_CONFIG_PROJECT_DISABLE='1',
               OPENCODE_CONFIG_DIR=str(config_path.parent / 'config'),
               OPENCODE_DISABLE_FILEWATCHER='1', OPENCODE_DISABLE_FFF='1',
               GIT_OPTIONAL_LOCKS='0')
    Path(env['OPENCODE_CONFIG_DIR']).mkdir(mode=0o700, exist_ok=True)
    prefix = Path(args.diagnostics)
    prefix.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(str(prefix) + '.server.err', 'w') as err:
        proc = subprocess.Popen(args.cli + ['serve', '--stdio', '--hostname', '127.0.0.1', '--port', '0'],
                                cwd=args.directory, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=err, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=30):
                    raise TimeoutError('private server did not become ready within 30 seconds')
                ready = json.loads(proc.stdout.readline())
            url = ready['url']
            if not url.startswith('http://127.0.0.1:'):
                raise ValueError('private server did not bind to loopback')

            def request(operation):
                result = subprocess.run(args.cli + ['api', '--server', url, operation,
                    '--param', f'location[directory]={args.directory}'],
                    env=env, cwd=args.directory, text=True, capture_output=True, timeout=60)
                if result.returncode:
                    Path(str(prefix) + '.preflight.err').write_text(result.stderr)
                    raise RuntimeError(f'{operation} preflight failed; see {prefix}.preflight.err')
                return json.loads(result.stdout)

            # Unlike agent.list, model.list waits for builtin/config plugins.
            # Listing the catalog performs no model inference.
            request('model.list')
            if args.agent:
                resolved = request('agent.list')
                Path(str(prefix) + '.agents.json').write_text(json.dumps(resolved))
                validate_agent(resolved, config, args.agent)
            return subprocess.run(args.cli + [url if x == '@SERVER@' else x for x in command],
                                  cwd=args.directory, env=env).returncode
        finally:
            # Closing our lease shuts down only this private server.
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc.stdout.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'opencode-private: {exc}', file=sys.stderr)
        sys.exit(2)
